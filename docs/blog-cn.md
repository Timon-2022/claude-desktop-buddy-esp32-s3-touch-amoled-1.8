# 给 Claude Desktop Buddy 加上用量监控和 GIF 动画角色

Claude Desktop Buddy 是一个运行在 ESP32-S3 上的桌面摆件，通过蓝牙连接 Claude Desktop，实时显示 AI 助手的工作状态。这篇文章记录了我在原版基础上做的三件事：

1. **BLE 用量监控**：通过 Mac 后台守护进程读取 Anthropic API 限速数据，经由蓝牙发送给设备
2. **GIF 角色包**：把原来的 ASCII 像素画替换成 clawd 动画 GIF
3. **UI 重设计**：重绘用量页面和主页文字区域，充分利用彩色 AMOLED 屏幕

---

## 硬件

- **Waveshare ESP32-S3 Touch AMOLED 1.8"**
- 物理分辨率：368×448（逻辑画布 184×224，固件做 2× 放大）
- 触摸屏，带加速度计和 RTC
- 通过 Nordic UART Service (NUS) 与 Mac 通信

---

## 一、BLE 用量监控

### 思路

Anthropic API 的每次响应 header 里会带有统一限速信息：

```
anthropic-ratelimit-unified-5h-utilization: 0.14
anthropic-ratelimit-unified-7d-utilization: 0.10
anthropic-ratelimit-unified-5h-reset: 1747448820.0
anthropic-ratelimit-unified-7d-reset: 1747944820.0
```

写一个 Mac 后台脚本，每 5 分钟用一个最小请求触发一次 API 调用，读取这些 header，通过蓝牙发给设备。

### OAuth 认证

不需要单独配置 API Key。直接复用 Claude Code 存在 macOS Keychain 里的 OAuth 凭据（服务名 `Claude Code-credentials`），支持自动刷新。首次运行会弹出浏览器完成 PKCE 登录，之后无需干预。

```python
KEYCHAIN_SERVICE = "Claude Code-credentials"
OAUTH_TOKEN_URL  = "https://console.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID  = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REDIRECT   = "http://localhost:54545/callback"
```

Token 交换和刷新均使用 **form-encoded**（非 JSON）POST，需要带 `anthropic-beta: oauth-2025-04-20` header。

### BLE 协议

设备端跑 Nordic UART Service，Mac 端用 bleak 连接。发送格式为一行 JSON：

```python
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # 写入
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # 通知

payload = {
    "rate_5h": 0.14,
    "rate_7d": 0.10,
    "rate_5h_reset_mins": 218,
    "rate_7d_reset_mins": 8303,
}
```

reset 时间从 Unix timestamp 换算：

```python
delta = float(reset_header) - time.time()
reset_mins = max(0, int(delta / 60))
```

设备端在 `data.h` 里解析：

```cpp
if (doc["rate_5h"].is<float>())
    out->rate5h = doc["rate_5h"].as<float>();
if (doc["rate_5h_reset_mins"].is<int32_t>())
    out->rate5hResetMins = doc["rate_5h_reset_mins"].as<int32_t>();
if (doc["rate_5h"].is<float>() || doc["rate_7d"].is<float>())
    out->rateUpdatedMs = millis();
```

### 多客户端 BLE

设备原本在第一个客户端连接后停止广播，导致 Mac 守护进程无法在 Claude Desktop 已连接的情况下再接入。在 `ble_bridge.cpp` 的 `onConnect` 回调里加一行即可：

```cpp
void onConnect(BLEServer* s) override {
    connected = true;
    BLEDevice::startAdvertising();  // 保持广播，允许第二个客户端连接
}
```

---

## 二、macOS 守护进程配置

### BLE 权限问题

macOS 的 LaunchAgent 默认跑在没有 GUI 的 session 里，CoreBluetooth 初始化会崩溃或报 "BLE is not authorized"。

解决方法：用 `osascript` 包装，让 Python 进程跑在 Aqua session 里。

```xml
<!-- ~/Library/LaunchAgents/com.claude.usage-bridge.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude.usage-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/osascript</string>
        <string>-e</string>
        <string>do shell script "/usr/bin/python3 -u '/Users/YOUR_NAME/Library/Application Support/ClaudeUsageBridge/usage_bridge.py' >> /tmp/usage_bridge.log 2>&1"</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/tmp/usage_bridge_osa.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/usage_bridge_osa.log</string>
</dict>
</plist>
```

`do shell script` 会在用户的 AppleScript Aqua session 里执行命令，CoreBluetooth 可以正常访问。`KeepAlive` 保证脚本退出后自动重启。

### 安装步骤

```bash
# 1. 安装依赖
pip3 install bleak requests

# 2. 复制脚本
mkdir -p ~/Library/Application\ Support/ClaudeUsageBridge
cp tools/usage_bridge.py ~/Library/Application\ Support/ClaudeUsageBridge/

# 3. 修改 plist 里的用户名，然后安装
cp docs/com.claude.usage-bridge.plist ~/Library/LaunchAgents/

# 4. 首次运行（触发浏览器登录和蓝牙权限弹窗）
/usr/bin/python3 -u ~/Library/Application\ Support/ClaudeUsageBridge/usage_bridge.py
# 登录完成后 Ctrl+C

# 5. 在系统设置 → 隐私与安全性 → 蓝牙 确认 Python 已授权

# 6. 启动 daemon
launchctl load ~/Library/LaunchAgents/com.claude.usage-bridge.plist
```

查看日志：

```bash
tail -f /tmp/usage_bridge.log
```

正常输出：

```
[poll] 22:36:07
[fetch] 5h=14%  7d=10%  reset5h=213m  reset7d=8303m
[ble] scanning for 'Claude-*' ...
[ble] found Claude-1715 (7BBEA5AB-E830-D67A-2004-99C9AB7462DF)
[ble] sent 82 bytes to Claude-1715
[poll] next in 297s
```

---

## 三、GIF 角色包

原版固件支持从 LittleFS 加载 GIF 动画，通过 Claude Desktop 的 Hardware Buddy 窗口传输。`tools/prep_character.py` 负责将任意 GIF 集合打包成设备格式。

### 角色来源

使用 [rullerzhou-afk/clawd-on-desk](https://github.com/rullerzhou-afk/clawd-on-desk) 的 7 个状态动画。

### 打包流程

`prep_character.py` 的核心思路：

1. 将所有状态的所有帧缩放到统一参考宽度（1000px）
2. 计算跨所有帧的全局 bounding box，保证各状态角色大小和位置一致
3. 按 `TARGET_W` 缩放输出，减色到 64 色调色板

```python
TARGET_W = 168  # 填满安全区域宽度（184px 画布，减去 SAFE_INSET=8 两侧）
```

输出 168×150px，在物理屏幕上显示为 336×300px（2× 放大），占满屏幕上半部分。

```bash
python3 tools/prep_character.py /path/to/clawd-source/
# 输出到 characters/clawd/，直接拖入 Hardware Buddy 窗口安装
```

### manifest.json

```json
{
  "name": "clawd",
  "colors": {
    "bg": "#000000",
    "text": "#ffffff",
    "body": "#ff9060",
    "textDim": "#666666"
  },
  "states": {
    "sleep":     "sleep.gif",
    "idle":      "idle.gif",
    "busy":      "busy.gif",
    "attention": "attention.gif",
    "celebrate": "celebrate.gif",
    "dizzy":     "dizzy.gif",
    "heart":     "heart.gif"
  }
}
```

---

## 四、UI 重设计

### 用量页面

参考 Clawdmeter 的设计风格，完全重绘，充分利用 AMOLED 纯黑背景。

布局（画布坐标，单位 px）：

```
y=8   ┌─[图标]──── Usage ────[电量]─┐
      │                              │
y=32  │  50%          [Current]      │
      │  ████████████████░░░░░░░░░░  │
      │  Resets in 3h 38m            │
      │                              │
y=88  ├──────────────────────────────┤
      │                              │
y=94  │  10%          [Weekly]       │
      │  ██░░░░░░░░░░░░░░░░░░░░░░░░  │
      │  Resets in 6d 8h             │
      │                              │
y=156 ├──────────────────────────────┤
      │                              │
y=186 │        * thinking...         │  ← size 2，居中
      └──────────────────────────────┘
```

关键设计决策：

- **纯黑背景**（`0x0000`）：AMOLED 关闭黑色像素，省电且对比度最高
- **颜色编码**：绿色 < 50%，橙色 50–80%，红色 > 80%
- **Pill 标签**：深紫色圆角矩形（`0x4010`）+ 浅灰文字，区分 5h/7d
- **状态栏**：底部 60px 专用区域，size 2 大字居中，橙色显示当前 Claude 状态

状态文字映射：

```cpp
case P_BUSY:      return "working...";
case P_ATTENTION: return "waiting...";
case P_SLEEP:     return "sleeping...";
default:          return "thinking...";
```

### 主页 HUD 文字区域

原版：3 行 × 10px 行高 × 22 字宽 = 34px，只用了屏幕底部一小条。

改后：5 行 × 11px 行高 × 26 字宽 = 63px，加细线分割符。

```cpp
const int SHOW = 5, LH = 11, WIDTH = 26;
const int AREA = SHOW * LH + 8;  // 63px
```

文字区从 y=190 提升到 y=161，宽度从 132px 扩展到 156px，可读性大幅改善。

---

## 整体效果

| 页面 | 主要内容 |
|------|---------|
| 主页 | clawd GIF 动画 + 底部 5 行会话文字 |
| 用量页 | 5h/7d 百分比 + 进度条 + 重置倒计时 + 状态 |

数据流：

```
Anthropic API
     │ rate-limit headers (每 5 分钟)
     ▼
usage_bridge.py (Mac LaunchAgent)
     │ JSON via BLE NUS
     ▼
ESP32-S3 (usage page)
```

所有配置一次完成，之后开机自动运行，无需任何手动操作。
