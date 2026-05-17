#!/usr/bin/env python3
"""
usage_bridge.py — Mac companion daemon for Claude Desktop Buddy.

Reads your Claude Code OAuth credentials from macOS Keychain, polls the
Anthropic API every 5 minutes to read rate-limit headers, then sends them
to the ESP32 device over BLE (Nordic UART Service).

The token is refreshed automatically when it expires — no manual action needed.
Refreshed tokens are written back to the Keychain so Claude Code sees them too.

Setup:
  pip3 install bleak requests   (one-time)
  python3 tools/usage_bridge.py

Optional env vars:
  BUDDY_BLE_NAME      override device name prefix (default "Claude-")
  POLL_INTERVAL       seconds between polls (default 300)
"""

import asyncio
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from typing import Optional

try:
    import requests
except ImportError:
    sys.exit("Missing dependency — run: pip3 install bleak requests")

try:
    from bleak import BleakScanner, BleakClient
    from bleak.exc import BleakError
except ImportError:
    sys.exit("Missing dependency — run: pip3 install bleak requests")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # client → device (WRITE)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → client (NOTIFY)

RATE_HDRS = [
    "anthropic-ratelimit-unified-5h-utilization",
    "anthropic-ratelimit-unified-7d-utilization",
    "anthropic-ratelimit-unified-5h-reset",
    "anthropic-ratelimit-unified-7d-reset",
]

OAUTH_TOKEN_URL   = "https://console.anthropic.com/v1/oauth/token"
OAUTH_AUTH_URL    = "https://claude.ai/oauth/authorize"
OAUTH_CLIENT_ID   = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REDIRECT    = "http://localhost:54545/callback"
OAUTH_SCOPES      = ["org:create_api_key", "user:profile", "user:inference"]
KEYCHAIN_SERVICE  = "Claude Code-credentials"

BUDDY_NAME_PREFIX = os.environ.get("BUDDY_BLE_NAME", "Claude-")
POLL_INTERVAL     = int(os.environ.get("POLL_INTERVAL", "300"))


# ---------------------------------------------------------------------------
# OAuth credential management
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest   = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def browser_login() -> dict:
    """
    Full PKCE browser OAuth flow.  Opens the consent URL, waits for the
    redirect callback on localhost:54545, exchanges code for tokens, and
    returns an oauth dict compatible with Claude Code's Keychain format.
    """
    verifier, challenge = _pkce_pair()
    state = secrets.token_hex(16)

    params = {
        "response_type":         "code",
        "client_id":             OAUTH_CLIENT_ID,
        "redirect_uri":          OAUTH_REDIRECT,
        "scope":                 " ".join(OAUTH_SCOPES),
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
    }
    auth_url = OAUTH_AUTH_URL + "?" + urllib.parse.urlencode(params)

    # Capture the redirect code in a local HTTP server on port 54545.
    _result: dict = {}
    _ready = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_): pass  # silence request logs
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _result["code"]  = qs.get("code",  [""])[0]
            _result["state"] = qs.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>Logged in! You can close this tab.</h2>")
            _ready.set()

    srv = http.server.HTTPServer(("127.0.0.1", 54545), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    print(f"\n[login] Opening browser for Claude login...")
    print(f"[login] If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    if not _ready.wait(timeout=120):
        srv.shutdown()
        raise TimeoutError("Login timed out (120s) — please try again")
    srv.shutdown()

    if _result.get("state") != state:
        raise ValueError("OAuth state mismatch — possible CSRF, aborting")

    code = _result.get("code", "")
    if not code:
        raise ValueError("No authorization code received")

    # Exchange code → tokens (form-encoded, not JSON)
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  OAUTH_REDIRECT,
            "client_id":     OAUTH_CLIENT_ID,
            "code_verifier": verifier,
            "state":         state,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:200]}")

    d = resp.json()
    expires_at = int(time.time() * 1000) + d["expires_in"] * 1000
    return {
        "accessToken":  d["access_token"],
        "refreshToken": d.get("refresh_token", ""),
        "expiresAt":    expires_at,
        "scopes":       d.get("scope", "").split(),
    }


def _keychain_read() -> dict:
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
    return json.loads(raw)


def _keychain_write(obj: dict) -> None:
    """Update the Claude Code Keychain entry with a new credentials blob."""
    value = json.dumps(obj)
    # Delete the old entry, then re-add with the new value.
    subprocess.run(
        ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE],
        stderr=subprocess.DEVNULL, check=False,
    )
    subprocess.run(
        ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
         "-a", "", "-w", value],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _refresh_token(refresh_token: str) -> dict:
    """
    Exchange a refresh token for a new access token.
    Returns the new oauth dict: {accessToken, refreshToken, expiresAt, ...}
    Raises on failure.
    """
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
            "client_id":     OAUTH_CLIENT_ID,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text[:200]}")
    d = resp.json()
    expires_at = int(time.time() * 1000) + d["expires_in"] * 1000
    return {
        "accessToken":  d["access_token"],
        "refreshToken": d.get("refresh_token", refresh_token),
        "expiresAt":    expires_at,
        "scopes":       d.get("scope", "").split(),
    }


def get_access_token() -> str:
    """
    Return a valid Claude Code access token.
    Tries refresh first; falls back to browser OAuth on failure.
    Saves updated tokens back to Keychain after every successful refresh.
    """
    try:
        creds = _keychain_read()
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        creds = {"claudeAiOauth": {}}

    oauth         = creds.get("claudeAiOauth", {})
    access_token  = oauth.get("accessToken", "")
    refresh_tok   = oauth.get("refreshToken", "")
    expires_at_ms = oauth.get("expiresAt", 0)
    now_ms        = time.time() * 1000
    needs_refresh = not access_token or (expires_at_ms - now_ms) < 5 * 60 * 1000

    if not needs_refresh:
        return access_token

    # Try silent refresh first
    new_oauth = None
    if refresh_tok:
        print("[auth] refreshing token...")
        try:
            new_oauth = _refresh_token(refresh_tok)
        except RuntimeError as e:
            print(f"[auth] refresh failed ({e})")

    # Fall back to browser login
    if new_oauth is None:
        try:
            new_oauth = browser_login()
        except Exception as e:
            sys.exit(f"[auth] login failed: {e}")

    oauth.update(new_oauth)
    creds["claudeAiOauth"] = oauth
    try:
        _keychain_write(creds)
        print("[auth] credentials saved to Keychain")
    except Exception as e:
        print(f"[auth] WARNING: could not save to Keychain: {e}")

    return new_oauth["accessToken"]


def make_auth_headers(access_token: str) -> dict:
    return {
        "Authorization":  f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
    }


# ---------------------------------------------------------------------------
# Rate-limit fetch
# ---------------------------------------------------------------------------

def _parse_iso8601(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fetch_rate_limits(access_token: str) -> Optional[dict]:
    """
    Hit the API with a minimal request and read the unified rate-limit headers.
    Returns {rate_5h, rate_7d, rate_5h_reset_mins, rate_7d_reset_mins} or None.
    """
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                **make_auth_headers(access_token),
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":     "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages":  [{"role": "user", "content": "hi"}],
            },
            timeout=20,
        )
    except requests.RequestException as e:
        print(f"[fetch] network error: {e}")
        return None

    if resp.status_code == 401:
        print("[fetch] 401 — token invalid, will refresh next cycle")
        return None

    h = resp.headers
    result: dict = {}

    v5h = h.get(RATE_HDRS[0])
    v7d = h.get(RATE_HDRS[1])
    r5h = h.get(RATE_HDRS[2])
    r7d = h.get(RATE_HDRS[3])

    if v5h is not None:
        try:   result["rate_5h"] = float(v5h)
        except ValueError: pass

    if v7d is not None:
        try:   result["rate_7d"] = float(v7d)
        except ValueError: pass

    now_ts = time.time()
    if r5h:
        try:
            delta = float(r5h) - now_ts
            result["rate_5h_reset_mins"] = max(0, int(delta / 60))
        except (ValueError, TypeError): pass

    if r7d:
        try:
            delta = float(r7d) - now_ts
            result["rate_7d_reset_mins"] = max(0, int(delta / 60))
        except (ValueError, TypeError): pass

    if not result:
        print(f"[fetch] no rate-limit headers (HTTP {resp.status_code}) — "
              "plan may not include unified rate limits")
        return None

    pct5 = f"{result['rate_5h']:.0%}" if "rate_5h" in result else "?"
    pct7 = f"{result['rate_7d']:.0%}" if "rate_7d" in result else "?"
    r5m  = result.get("rate_5h_reset_mins", "?")
    r7m  = result.get("rate_7d_reset_mins", "?")
    print(f"[fetch] 5h={pct5}  7d={pct7}  reset5h={r5m}m  reset7d={r7m}m")
    return result


# ---------------------------------------------------------------------------
# BLE send
# ---------------------------------------------------------------------------

async def _find_device(timeout: float = 10.0):
    print(f"[ble] scanning for '{BUDDY_NAME_PREFIX}*' ...")
    try:
        devices = await BleakScanner.discover(timeout=timeout)
    except BleakError as e:
        print(f"[ble] scan error: {e}")
        return None
    for d in devices:
        if (d.name or "").startswith(BUDDY_NAME_PREFIX):
            print(f"[ble] found {d.name} ({d.address})")
            return d
    return None


async def send_via_ble(payload: dict, retries: int = 3) -> bool:
    device = await _find_device()
    if device is None:
        print("[ble] device not found — make sure it's on, in range, and Mac Bluetooth is enabled")
        return False

    line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()

    for attempt in range(1, retries + 1):
        try:
            async with BleakClient(device, timeout=15.0) as client:
                await client.start_notify(NUS_TX, lambda _, __: None)
                chunk = 182
                for i in range(0, len(line), chunk):
                    await client.write_gatt_char(NUS_RX, line[i:i+chunk], response=False)
                await asyncio.sleep(0.5)
                print(f"[ble] sent {len(line)} bytes to {device.name}")
                return True
        except BleakError as e:
            print(f"[ble] attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                await asyncio.sleep(3)

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main():
    print(f"[init] polling every {POLL_INTERVAL}s, device prefix '{BUDDY_NAME_PREFIX}'")

    while True:
        t0 = time.monotonic()
        print(f"\n[poll] {datetime.now().strftime('%H:%M:%S')}")

        # Re-fetch access token each cycle (auto-refreshes when near expiry)
        try:
            token = get_access_token()
        except SystemExit as e:
            print(f"[auth] fatal: {e}")
            break

        data = fetch_rate_limits(token)
        if data:
            await send_via_ble(data)
        else:
            print("[poll] no data to send this cycle")

        sleep_s = max(0, POLL_INTERVAL - (time.monotonic() - t0))
        print(f"[poll] next in {sleep_s:.0f}s")
        await asyncio.sleep(sleep_s)


if __name__ == "__main__":
    asyncio.run(main())
