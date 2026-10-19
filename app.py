#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vycebump - Vyce.cloud 免费 VPS 自动续期与保活工具
灵感与架构源自 katabump (https://github.com/dengyie/katabump)

核心特性:
  1. 纯 Python 3 原生标准库实现 (零第三方依赖，无需 pip install)，开箱即用
  2. 支持 GitHub Actions 云端全自动调度运行 (Cron / Dispatch)
  3. 支持本地一键执行，自动通过 Chrome 9222 CDP 提取最新活跃登录 Cookie
  4. 精准计算 24 小时续期窗口与冷却时间，避免无效频繁请求
  5. 支持 Telegram 机器人通知推送 (账户脱敏、机器状态、到期预警)
  6. 状态持久化 (renew_state.json) 跨 run 缓存，支持多账号
"""

import os
import sys
import json
import time
import ssl
import subprocess
import datetime
import urllib.request
import urllib.error
import urllib.parse
from typing import Dict, List, Optional, Tuple

# ============================================================
# 配置与环境变量
# ============================================================
BASE_URL = "https://vyce.cloud"
API_BASE = f"{BASE_URL}/api"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
CDP_URL = os.environ.get("CDP_URL") or "http://127.0.0.1:9222"
FORCE_RENEW = os.environ.get("FORCE_RENEW", "").lower() in ("true", "1", "yes")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(SCRIPT_DIR, "renew_state.json")

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36"
)

# 冷却期常量 (毫秒)
ONE_HOUR_MS = 3600 * 1000
ONE_DAY_MS = 24 * ONE_HOUR_MS


# ============================================================
# HTTP 请求辅助函数 (原生 urllib 实现，支持代理与 SSL)
# ============================================================
def http_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    proxy: Optional[str] = None,
    timeout: int = 20,
) -> Tuple[int, Dict[str, str], str]:
    """
    通用 HTTP 请求封装
    返回: (status_code, response_headers, response_body_text)
    """
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    # 使用系统标准 CA 证书校验 HTTPS，防范中间人攻击
    ctx = ssl.create_default_context()
    handlers.append(urllib.request.HTTPSHandler(context=ctx))

    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=data, method=method)

    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
            resp_headers = dict(resp.headers)
            body = resp.read().decode("utf-8", errors="replace")
            return status, resp_headers, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, dict(e.headers), body
    except Exception as e:
        return 0, {}, f"Request Exception: {str(e)}"


# ============================================================
# Telegram 消息推送
# ============================================================
def mask_username(username: str) -> str:
    """账号脱敏: dengyie -> de****ie"""
    if not username:
        return "未知用户"
    if "@" in username:
        name, domain = username.split("@", 1)
        if len(name) > 4:
            return f"{name[:2]}****{name[-2:]}@{domain}"
        return f"{name}@{domain}"
    if len(username) > 4:
        return f"{username[:2]}****{username[-2:]}"
    return username


def send_tg_message(
    status_icon: str,
    status_title: str,
    account_name: str,
    details: Optional[Dict] = None,
    error_msg: str = "",
    proxy: Optional[str] = None,
):
    """发送 Telegram 通知"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    beijing_time = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=8))
    ).strftime("%Y-%m-%d %H:%M:%S")

    masked_acc = mask_username(account_name)
    lines = [
        "🇪🇺 <b>Vyce.cloud 续期通知</b>",
        "",
        f"{status_icon} <b>{status_title}</b>",
        f"👤 账户: <code>{masked_acc}</code>",
    ]

    if details:
        if details.get("server_name"):
            lines.append(f"🖥️ 服务器: <code>{details['server_name']}</code>")
        if details.get("plan_id"):
            lines.append(f"📦 规格: {details['plan_id']} ({details.get('os', '')})")
        if details.get("ipv6"):
            lines.append(f"🌐 IPv6: <code>{details['ipv6']}</code>")
        if details.get("time_left_str"):
            lines.append(f"⏳ 剩余存活: <b>{details['time_left_str']}</b>")
        if details.get("next_renewal_str"):
            lines.append(f"⏰ 续期时间窗口: {details['next_renewal_str']}")

    if error_msg:
        lines.append(f"⚠️ 详情: {error_msg}")

    lines.append(f"⏱️ 报告时间: {beijing_time} (UTC+8)")

    text = "\n".join(lines)
    tg_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    status, _, res = http_request(tg_url, method="POST", headers=headers, data=payload, proxy=proxy, timeout=15)
    if status == 200:
        print("📩 Telegram 通知发送成功！")
    else:
        print(f"⚠️ Telegram 发送失败 ({status}): {res}")


# ============================================================
# 本地 Chrome CDP Cookie 提取
# ============================================================
def get_cookie_from_cdp(cdp_base_url: str = CDP_URL) -> Optional[str]:
    """通过本地 Chrome 9222 CDP 接口自动提取活跃的 __Host-vyce_sid Cookie"""
    json_url = f"{cdp_base_url.rstrip('/')}/json"
    status, _, body = http_request(json_url, timeout=3)
    if status != 200:
        return None

    try:
        tabs = json.loads(body)
    except Exception:
        return None

    # 寻找 vyce.cloud 相关的 tab
    target_tab = None
    for tab in tabs:
        url = tab.get("url", "")
        if "vyce.cloud" in url and tab.get("webSocketDebuggerUrl"):
            target_tab = tab
            break

    if not target_tab and tabs:
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                target_tab = tab
                break

    if not target_tab:
        return None

    ws_url = target_tab.get("webSocketDebuggerUrl", "")
    if not ws_url or not (ws_url.startswith("ws://") or ws_url.startswith("wss://")):
        return None

    # 利用本地 node 执行单次 WebSocket 提取，通过命令行参数传递 wsUrl，防止代码插值注入
    node_code = """
    const wsUrl = process.argv[1];
    if (!wsUrl) process.exit(1);
    const ws = new WebSocket(wsUrl);
    ws.addEventListener("open", () => {
      ws.send(JSON.stringify({
        id: 1,
        method: "Network.getCookies",
        params: { urls: ["https://vyce.cloud"] }
      }));
    });
    ws.addEventListener("message", (event) => {
      const res = JSON.parse(event.data);
      const cookies = res.result?.cookies || [];
      const target = cookies.find(c => c.name === "__Host-vyce_sid");
      if (target) console.log("__Host-vyce_sid=" + target.value);
      ws.close();
      process.exit(0);
    });
    ws.addEventListener("error", () => {
      process.exit(1);
    });
    """
    try:
        p = subprocess.run(
            ["node", "--input-type=module", "-e", node_code, ws_url],
            capture_output=True,
            text=True,
            timeout=8
        )
        out = p.stdout.strip()
        if "__Host-vyce_sid=" in out:
            return out
    except FileNotFoundError:
        print("ℹ️ 本地环境未检测到 node 命令，跳过 CDP WebSocket 自动化提取。")
    except Exception as e:
        print(f"⚠️ CDP 提取异常: {e}")

    return None


# ============================================================
# 账号加载模块
# ============================================================
def load_accounts() -> List[Dict[str, str]]:
    """
    支持多种账号输入形式:
      1. USERS_JSON: '[{"cookie": "__Host-vyce_sid=...", "name": "dengyie"}]'
      2. VYCE_COOKIE: '__Host-vyce_sid=...' 或纯 Token 字符串
      3. 本地 Chrome CDP (9222 端口) 自动探测
    """
    accounts = []

    # 1. USERS_JSON
    raw_users = os.environ.get("USERS_JSON", "").strip()
    if raw_users:
        try:
            parsed = json.loads(raw_users)
            if isinstance(parsed, list):
                for item in parsed:
                    cookie = item.get("cookie") or item.get("token") or ""
                    if cookie:
                        if not cookie.startswith("__Host-vyce_sid="):
                            cookie = f"__Host-vyce_sid={cookie}"
                        accounts.append({
                            "name": item.get("name") or item.get("login") or "VyceUser",
                            "cookie": cookie
                        })
        except Exception as e:
            print(f"⚠️ USERS_JSON 解析出错: {e}")

    # 2. 单账号 VYCE_COOKIE
    single_cookie = os.environ.get("VYCE_COOKIE", "").strip()
    if single_cookie:
        if not single_cookie.startswith("__Host-vyce_sid="):
            single_cookie = f"__Host-vyce_sid={single_cookie}"
        accounts.append({
            "name": os.environ.get("VYCE_USER", "VyceUser"),
            "cookie": single_cookie
        })

    # 3. 本地 CDP 探测
    if not accounts:
        print("🔍 尝试从本地 Chrome 9222 CDP 探测活跃登录态...")
        cdp_cookie = get_cookie_from_cdp(CDP_URL)
        if cdp_cookie:
            print("✅ 成功从本地 Chrome 提取到最新 Session Cookie！")
            accounts.append({
                "name": "LocalChromeUser",
                "cookie": cdp_cookie
            })

    return accounts


# ============================================================
# 时间格式化辅助
# ============================================================
def format_duration(seconds: float) -> str:
    """把秒数格式化为 1d 12h 或 12h 20m 格式"""
    total_sec = max(0, int(seconds))
    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60

    if days >= 1:
        return f"{days}d {hours}h"
    if hours >= 1:
        return f"{hours}h {minutes:02d}m"
    if minutes >= 1:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def ms_to_beijing_str(timestamp_ms: Optional[int]) -> str:
    if not timestamp_ms:
        return "未知"
    dt = datetime.datetime.fromtimestamp(
        timestamp_ms / 1000.0,
        tz=datetime.timezone(datetime.timedelta(hours=8))
    )
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# Vyce.cloud API 交互
# ============================================================
class VyceClient:
    def __init__(self, cookie: str, proxy: Optional[str] = None):
        self.cookie = cookie
        self.proxy = proxy
        self.headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/",
            "Cookie": cookie,
        }

    def get_me(self) -> Optional[Dict]:
        """获取当前用户信息: GET /api/me"""
        url = f"{API_BASE}/me"
        status, _, body = http_request(url, headers=self.headers, proxy=self.proxy, timeout=15)
        if status == 200:
            try:
                data = json.loads(body)
                return data.get("user")
            except Exception:
                return None
        print(f"⚠️ GET /api/me 失败 (HTTP {status}): {body}")
        return None

    def get_servers(self) -> List[Dict]:
        """获取服务器列表: GET /api/servers"""
        url = f"{API_BASE}/servers"
        status, _, body = http_request(url, headers=self.headers, proxy=self.proxy, timeout=15)
        if status == 200:
            try:
                data = json.loads(body)
                return data.get("servers", [])
            except Exception:
                return []
        print(f"⚠️ GET /api/servers 失败 (HTTP {status}): {body}")
        return []

    def renew_server(self, server_id: str) -> Tuple[bool, int, str]:
        """
        续期服务器: POST /api/servers/{id}/renew
        返回: (成功布尔值, HTTP状态码, 响应消息)
        """
        url = f"{API_BASE}/servers/{server_id}/renew"
        status, _, body = http_request(url, method="POST", headers=self.headers, proxy=self.proxy, timeout=20)
        try:
            res_json = json.loads(body)
        except Exception:
            res_json = {}

        if status in (200, 204):
            msg = res_json.get("message") or "Renewed successfully"
            return True, status, msg

        msg = res_json.get("message") or res_json.get("error") or body
        return False, status, msg


# ============================================================
# 状态持久化与单服务器续期逻辑
# ============================================================
def load_state() -> Dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: Dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 保存状态文件失败: {e}")


def process_account(account: Dict, state: Dict, proxy: Optional[str] = None) -> bool:
    cookie = account["cookie"]
    account_label = account.get("name", "VyceUser")

    print(f"\n==================================================")
    print(f"🔑 正在处理账户: {mask_username(account_label)}")
    print(f"==================================================")

    client = VyceClient(cookie, proxy=proxy)
    user_info = client.get_me()

    if not user_info:
        print("❌ 账户登录失效或 Cookie 无效，请更新 __Host-vyce_sid Cookie！")
        send_tg_message(
            status_icon="❌",
            status_title="Cookie 失效 / 登录失败",
            account_name=account_label,
            error_msg="无法通过 /api/me 获取用户信息，请检查或重新获取 __Host-vyce_sid。",
            proxy=proxy
        )
        return False

    real_username = user_info.get("login") or account_label
    print(f"✅ 登录成功！用户: {real_username} (ID: {user_info.get('id')})")

    servers = client.get_servers()
    if not servers:
        print("ℹ️ 该账户名下无任何服务器。")
        return True

    now_ms = int(time.time() * 1000)
    account_ok = True

    for srv in servers:
        srv_id = srv.get("id")
        srv_name = srv.get("name", "Unnamed")
        source = srv.get("source", "")
        plan_id = srv.get("planId", "")
        status = srv.get("status", "")
        ipv6 = srv.get("ipv6", "无")
        expires_at = srv.get("expiresAt") or 0

        print(f"\n🖥️ 检查服务器: {srv_name} ({srv_id})")
        print(f"   类型: {source} ({plan_id}) | 状态: {status} | IPv6: {ipv6}")

        # 仅针对免费服务器执行 24h 续期逻辑
        if source != "free":
            print(f"   ℹ️ 非免费机型 (source={source})，跳过续期。")
            continue

        # 读取并比对本地持久化缓存
        cached_entry = state.get(srv_id)
        if cached_entry and cached_entry.get("next_renewal_ts"):
            cached_next = cached_entry.get("next_renewal_ts")
            print(f"   📋 缓存记录下次续期: {ms_to_beijing_str(cached_next)}")

        # 计算剩余存活时间与冷却时间
        time_left_ms = expires_at - now_ms
        time_left_sec = time_left_ms / 1000.0
        time_left_str = format_duration(time_left_sec)

        # 冷却判定: 官方规则 expiresAt 初始为 48h，当 time_left > 24h 时处于 Cooldown 状态
        cooldown_left_ms = time_left_ms - ONE_DAY_MS
        cooldown_left_sec = cooldown_left_ms / 1000.0
        in_cooldown = cooldown_left_ms > 0 and status == "running"

        next_avail_ts = now_ms + cooldown_left_ms if in_cooldown else now_ms
        next_avail_str = ms_to_beijing_str(next_avail_ts)

        details = {
            "server_name": srv_name,
            "plan_id": plan_id,
            "os": srv.get("os", ""),
            "ipv6": ipv6,
            "time_left_str": time_left_str,
            "next_renewal_str": next_avail_str,
        }

        print(f"   ⏳ 剩余存活时间: {time_left_str} (到期: {ms_to_beijing_str(expires_at)})")

        if in_cooldown and not FORCE_RENEW:
            cd_str = format_duration(cooldown_left_sec)
            print(f"   ❄️ 当前处于冷却期中 (Cooldown)，无需续期。")
            print(f"   ⏰ 预计下次可续期时间: {next_avail_str} (约 {cd_str} 后)")
            
            # 更新状态记录
            state[srv_id] = {
                "name": srv_name,
                "user": real_username,
                "status": "cooldown",
                "time_left_str": time_left_str,
                "next_renewal_ts": next_avail_ts,
                "next_renewal_str": next_avail_str,
                "last_check_ts": now_ms,
            }
            continue

        # 满足续期条件 (进入可续期窗口 或 机器已停止 或 强制续期)
        print(f"   🚀 进入续期窗口或被强制触发，发起续期请求...")
        success, code, msg = client.renew_server(srv_id)

        if success:
            print(f"   🎉 续期成功！({msg})")
            # 重新拉取服务器最新状态
            fresh_servers = client.get_servers()
            fresh_srv = next((s for s in fresh_servers if s.get("id") == srv_id), None)
            if fresh_srv:
                new_expires = fresh_srv.get("expiresAt") or 0
                new_left_sec = (new_expires - now_ms) / 1000.0
                details["time_left_str"] = format_duration(new_left_sec)
                details["next_renewal_str"] = ms_to_beijing_str(now_ms + ONE_DAY_MS)
                print(f"   ✨ 机器存活已重置为: {details['time_left_str']} (到期: {ms_to_beijing_str(new_expires)})")

            state[srv_id] = {
                "name": srv_name,
                "user": real_username,
                "status": "success",
                "time_left_str": details["time_left_str"],
                "last_renew_ts": now_ms,
                "last_renew_str": ms_to_beijing_str(now_ms),
                "next_renewal_ts": now_ms + ONE_DAY_MS,
                "next_renewal_str": ms_to_beijing_str(now_ms + ONE_DAY_MS),
            }

            send_tg_message(
                status_icon="🎉",
                status_title="Vyce VPS 续期成功",
                account_name=real_username,
                details=details,
                proxy=proxy
            )
        else:
            if code == 429:
                print(f"   ⏳ 官方服务端提示冷却未完成 (429): {msg}")
                state[srv_id] = {
                    "name": srv_name,
                    "user": real_username,
                    "status": "cooldown_429",
                    "msg": msg,
                    "last_check_ts": now_ms,
                }
            else:
                print(f"   ❌ 续期失败 (HTTP {code}): {msg}")
                account_ok = False
                state[srv_id] = {
                    "name": srv_name,
                    "user": real_username,
                    "status": "failed",
                    "error": msg,
                    "last_check_ts": now_ms,
                }
                send_tg_message(
                    status_icon="❌",
                    status_title="Vyce VPS 续期失败",
                    account_name=real_username,
                    details=details,
                    error_msg=f"HTTP {code}: {msg}",
                    proxy=proxy
                )

    return account_ok


# ============================================================
# 主执行入口
# ============================================================
def main():
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] vycebump 保活脚本启动")

    proxy_server = os.environ.get("PROXY_SERVER") or os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy_server:
        print(f"🌐 使用代理出口: {proxy_server}")

    accounts = load_accounts()
    if not accounts:
        print("❌ 未检测到任何 Vyce.cloud 账号！")
        print("请通过以下方式之一配置凭证:")
        print("  1. 环境变量 VYCE_COOKIE='__Host-vyce_sid=...'")
        print("  2. 环境变量 USERS_JSON='[{\"cookie\": \"...\", \"name\": \"...\"}]'")
        print("  3. 本机启动 Chrome 并启用调试端口 (--remote-debugging-port=9222)")
        sys.exit(1)

    state = load_state()
    all_success = True

    for acc in accounts:
        try:
            ok = process_account(acc, state, proxy=proxy_server)
            if not ok:
                all_success = False
        except Exception as e:
            print(f"❌ 处理账户 {acc.get('name')} 时出现未捕获异常: {e}")
            all_success = False

    save_state(state)
    print(f"\n✅ 任务处理完毕，状态已保存至 {STATE_FILE}。")
    if not all_success:
        print("❌ 检测到有账户处理失败或续期异常，以非零状态码退出 (exit 1)。")
        sys.exit(1)


if __name__ == "__main__":
    main()
