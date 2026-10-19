# Vycebump Server Auto-Renewal Tool

[![GitHub Actions](https://img.shields.io/badge/GitHub_Actions-Auto_Renew-blue.svg)](https://github.com/dengyie/vycebump)
[![Python](https://img.shields.io/badge/Python-3.8+-green.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

专为 [Vyce.cloud](https://vyce.cloud/#/servers) 免费 VPS 设计的自动续期与保活工具，架构与理念灵感源自 [dengyie/katabump](https://github.com/dengyie/katabump)。

---

## 💡 为什么需要自动续期？

Vyce.cloud 为免费 VPS（如 `Vps-Mini`，1 vCore / 2 GB RAM / 6 GB NVMe / 纯 IPv6）提供永久免费使用资格，但设有以下保活机制：
1. **48 小时生命周期**：每台免费 VPS 创建或续期后，存活时间上限为 48 小时（2 天）。
2. **24 小时冷却保护**：每 24 小时只能续期一次。存活时间大于 24 小时时，面板处于 `Cooldown`（冷却中）状态，后台接口返回 `429 renewal_cooldown`。
3. **续期重置**：在存活时间进入最后 24 小时（或机器已停止）时点击续期，存活时间立即重新重置回 48 小时。
4. **逾期关机与销毁**：若 48 小时内未续期，机器关机保留文件；关机超过 3 天未续期则彻底销毁释放。

**Vycebump** 自动计算冷却倒计时，在进入 24h 续期时间窗口后自动触发官方 REST 接口，实现 365 天无感自动化续期与 Telegram 通知。

---

## ✨ 核心特性

- **零依赖原生 Python**：基于 Python 3 原生标准库（`urllib`、`ssl`、`json`）开发，无需 `pip install` 任何额外库，开箱即用。
- **双模驱动执行**：
  - **模式 A（本地 CDP 无感捕获）**：Mac / Linux 本机 Chrome 启动 `--remote-debugging-port=9222` 时，脚本自动通过 Chrome DevTools Protocol 动态抓取最新 `__Host-vyce_sid` 活跃 Cookie，免除手动复制粘贴。
  - **模式 B（云端 GitHub Actions）**：配置 GitHub Secret 后，每天全自动定时巡检与续期。
- **状态跨 Run 持久化**：内置 `renew_state.json` 缓存记录，冷却期内快速跳过，不浪费 GitHub Actions 配额或触发 Cloudflare 频控。
- **全协议代理支持**：内置 `proxy_handler.py`，支持 `sing-box` 托管（VLESS、VMess、Hysteria2、TUIC、SOCKS5、HTTP 等），方便网络受限环境穿透。
- **Telegram 实时推送**：支持账户脱敏、服务器信息、剩余存活时间、下次续期窗口预警与错误推送。

---

## 🚀 快速上手

### 方式一：GitHub Actions 云端自动调度 (推荐)

最省心、零成本的保活方式，配置一次永久生效：

1. **新建或 Fork 本仓库** 到你的 GitHub。
2. 进入仓库 **Settings** -> **Secrets and variables** -> **Actions**。
3. 新增以下 Repository Secret：
   - **`VYCE_COOKIE`**（必填）：填入 Vyce.cloud 的登录 Cookie，格式为 `__Host-vyce_sid=xxxx...`（在浏览器 F12 -> Application -> Cookies -> `https://vyce.cloud` 中获取，或从网络请求 Header 中复制）。
   - **`TG_BOT_TOKEN`**（可选）：Telegram Bot Token（由 [@BotFather](https://t.me/BotFather) 获取）。
   - **`TG_CHAT_ID`**（可选）：你的 Telegram 接收通知 Chat ID。
   - **`PROXY_URL`**（可选）：如需通过特定代理出口外连，填入节点链接（如 `vless://...`、`hy2://...`、`socks5://...`）。
4. （可选多账号）：如果拥有多个 Vyce 账号，可配置 **`USERS_JSON`** Secret：
   ```json
   [
     {"name": "user1", "cookie": "__Host-vyce_sid=cookie1..."},
     {"name": "user2", "cookie": "__Host-vyce_sid=cookie2..."}
   ]
   ```
5. 工作流默认每 4 小时检查一次（`0 */4 * * *`），可在 Actions 页面点击 **Run workflow** 进行首次手动测试。

---

### 方式二：本地运行 (搭配 Chrome 9222 端口)

若你在本地桌面 Chrome 中已登录 `https://vyce.cloud/#/servers`，且 Chrome 开启了远程调试端口：

```bash
# 1. 运行续期脚本 (自动检测 9222 端口并提取活跃 Cookie)
python3 app.py
```

终端输出示例：
```text
[2026-10-19 12:15:11] vycebump 保活脚本启动
🔍 尝试从本地 Chrome 9222 CDP 探测活跃登录态...
✅ 成功从本地 Chrome 提取到最新 Session Cookie！

==================================================
🔑 正在处理账户: dengyie
==================================================
✅ 登录成功！用户: dengyie (ID: 3e04604a-776e-41d2-9d7b-ae5f93f5c5b8)

🖥️ 检查服务器: mini-01 (eace8427-7b0b-4b2d-80ee-cba050946c00)
   类型: free (mini) | 状态: running | IPv6: 2a11:6c7:1105:1906::250
   ⏳ 剩余存活时间: 1d 12h (到期: 2026-10-21 00:27:23)
   ❄️ 当前处于冷却期中 (Cooldown)，无需续期。
   ⏰ 预计下次可续期时间: 2026-10-20 00:27:23 (约 12h 12m 后)

✅ 任务处理完毕，状态已保存至 renew_state.json。
```

---

## 🛠️ 环境变量对照表

| 变量名 | 说明 | 示例 | 默认值 |
| :--- | :--- | :--- | :--- |
| `VYCE_COOKIE` | 单账号登录 Cookie | `__Host-vyce_sid=ICYUl...` | 空 |
| `USERS_JSON` | 多账号 JSON 数组 | `[{"name":"..","cookie":".."}]` | 空 |
| `CDP_URL` | 本地 Chrome 调试端口 | `http://127.0.0.1:9222` | `http://127.0.0.1:9222` |
| `FORCE_RENEW` | 强制发起续期请求 (无视冷却) | `true` / `false` | `false` |
| `TG_BOT_TOKEN` | Telegram 通知 Bot Token | `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11` | 空 |
| `TG_CHAT_ID` | Telegram 接收通知的 Chat ID | `987654321` | 空 |
| `PROXY_SERVER` | HTTP/SOCKS 代理服务器 | `http://127.0.0.1:8080` | 空 |
| `PROXY_URL` | 全协议节点订阅/分享链接 (Actions sing-box) | `hy2://pwd@host:port?sni=...` | 空 |

---

## 📄 许可证

本项目依据 MIT License 开源。仅供个人自动化管理与技术交流学习使用。
