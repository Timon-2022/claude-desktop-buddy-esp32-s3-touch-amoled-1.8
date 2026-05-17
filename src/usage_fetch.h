// usage_fetch.h — polls Anthropic API rate-limit headers over WiFi.
// Include from exactly one translation unit (main.cpp).
#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <time.h>
#include "secrets.h"

struct UsageState {
  float   util5h;       // 0.0–1.0, negative = unknown
  float   util7d;
  int32_t reset5hMins;  // minutes until reset, -1 = unknown
  int32_t reset7dMins;
  bool    valid;
  bool    wifiConnected;
  uint32_t lastFetchMs;
};

static UsageState      _usageState  = { -1, -1, -1, -1, false, false, 0 };
static SemaphoreHandle_t _usageMtx  = nullptr;

inline const UsageState& usageGet() { return _usageState; }

// Parse ISO-8601 UTC string "2026-05-17T12:30:00Z" → epoch seconds.
static time_t _parseIso8601(const char* s) {
  struct tm t = {};
  int yr, mo, dy, hr, mn, sc;
  if (sscanf(s, "%d-%d-%dT%d:%d:%dZ", &yr, &mo, &dy, &hr, &mn, &sc) != 6) return 0;
  t.tm_year = yr - 1900;
  t.tm_mon  = mo - 1;
  t.tm_mday = dy;
  t.tm_hour = hr;
  t.tm_min  = mn;
  t.tm_sec  = sc;
  // mktime interprets as local time; with TZ=UTC0 that equals UTC epoch.
  return mktime(&t);
}

static const char* _RATE_HDRS[] = {
  "anthropic-ratelimit-unified-5h-utilization",
  "anthropic-ratelimit-unified-7d-utilization",
  "anthropic-ratelimit-unified-5h-reset",
  "anthropic-ratelimit-unified-7d-reset",
};

static void _usageFetchTask(void*) {
  // Ensure mktime works correctly as UTC.
  setenv("TZ", "UTC0", 1);
  tzset();

  for (;;) {
    // Wait for WiFi.
    while (WiFi.status() != WL_CONNECTED) {
      xTaskNotifyStateClear(nullptr);
      xSemaphoreTake(_usageMtx, 0);
      _usageState.wifiConnected = false;
      xSemaphoreGive(_usageMtx);
      vTaskDelay(pdMS_TO_TICKS(5000));
    }

    {
      xSemaphoreTake(_usageMtx, portMAX_DELAY);
      _usageState.wifiConnected = true;
      xSemaphoreGive(_usageMtx);
    }

    WiFiClientSecure client;
    client.setInsecure();   // skip cert verification (device has no root-CA store)

    HTTPClient http;
    if (http.begin(client, "https://api.anthropic.com/v1/messages")) {
      http.addHeader("x-api-key",         ANTHROPIC_API_KEY);
      http.addHeader("anthropic-version", "2023-06-01");
      http.addHeader("content-type",      "application/json");
      http.collectHeaders(_RATE_HDRS, 4);
      http.setTimeout(15000);

      // Cheapest possible request — just to get rate-limit response headers.
      int code = http.POST(
        "{\"model\":\"claude-haiku-4-5-20251001\","
        "\"max_tokens\":1,"
        "\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"
      );

      if (code > 0) {
        String h5h      = http.header(_RATE_HDRS[0]);
        String h7d      = http.header(_RATE_HDRS[1]);
        String h5hReset = http.header(_RATE_HDRS[2]);
        String h7dReset = http.header(_RATE_HDRS[3]);

        float   util5h = (h5h.length() > 0) ? h5h.toFloat() : -1.0f;
        float   util7d = (h7d.length() > 0) ? h7d.toFloat() : -1.0f;

        time_t  now    = time(nullptr);
        int32_t r5h = -1, r7d = -1;
        if (h5hReset.length() > 0) {
          time_t reset = _parseIso8601(h5hReset.c_str());
          r5h = (reset > now) ? (int32_t)((reset - now) / 60) : 0;
        }
        if (h7dReset.length() > 0) {
          time_t reset = _parseIso8601(h7dReset.c_str());
          r7d = (reset > now) ? (int32_t)((reset - now) / 60) : 0;
        }

        xSemaphoreTake(_usageMtx, portMAX_DELAY);
        _usageState.util5h      = util5h;
        _usageState.util7d      = util7d;
        _usageState.reset5hMins = r5h;
        _usageState.reset7dMins = r7d;
        _usageState.valid       = true;
        _usageState.lastFetchMs = millis();
        xSemaphoreGive(_usageMtx);
      }
      http.end();
    }

    // Poll every 5 minutes.
    vTaskDelay(pdMS_TO_TICKS(5UL * 60UL * 1000UL));
  }
}

inline void usageInit() {
  _usageMtx = xSemaphoreCreateMutex();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  // Run on core 0 (display loop runs on core 1) to avoid blocking animation.
  xTaskCreatePinnedToCore(_usageFetchTask, "usage_poll", 8192, nullptr, 1, nullptr, 0);
}
