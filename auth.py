import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv, set_key
import httpx
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

TOKEN_FILE = ".env"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL = "https://auth.openai.com"
AUTH_API_BASE_URL = f"{AUTH_BASE_URL}/api/accounts"
CHATGPT_LOGIN_URL = "https://chatgpt.com/auth/login"
CHATGPT_SESSION_URL = "https://chatgpt.com/api/auth/session"
DEVICE_AUTH_URL = f"{AUTH_BASE_URL}/codex/device"
DEVICE_AUTH_CALLBACK_URL = f"{AUTH_BASE_URL}/deviceauth/callback"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
AUTH_TIMEOUT_SECONDS = 15 * 60
SUPPORTED_AUTH_METHODS = {"auto", "browser", "device", "prompt"}


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}

        padding = "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(parts[1] + padding)
        return json.loads(decoded)
    except Exception:
        return {}


def _token_is_valid(token: str | None) -> bool:
    if not token or len(token) <= 100:
        return False

    payload = _decode_jwt_payload(token)
    exp = payload.get("exp")

    try:
        return int(exp) > int(time.time()) + 60
    except Exception:
        return True


def _load_codex_access_token():
    try:
        if not CODEX_AUTH_FILE.exists():
            return None

        data = json.loads(CODEX_AUTH_FILE.read_text())
        token = data.get("tokens", {}).get("access_token")
        if _token_is_valid(token):
            return token
    except Exception:
        return None

    return None


def _persist_access_token(token: str, source: str) -> None:
    set_key(TOKEN_FILE, "CHATGPT_ACCESS_TOKEN", token)
    set_key(TOKEN_FILE, "CHATGPT_ACCESS_TOKEN_SOURCE", source)


def get_access_token():
    load_dotenv(override=True)
    token = os.getenv("CHATGPT_ACCESS_TOKEN")
    if _token_is_valid(token):
        return token
    return None


def _normalize_auth_method(method: str | None) -> str:
    if not method:
        return "prompt"

    normalized = method.strip().lower()
    if normalized in SUPPORTED_AUTH_METHODS:
        return normalized

    return "prompt"


def _get_auth_method() -> str:
    load_dotenv(override=True)
    return _normalize_auth_method(os.getenv("CHATGPT_AUTH_METHOD", "prompt"))


def _is_headless_environment() -> bool:
    if os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"):
        return False

    if sys.platform.startswith("linux"):
        return True

    return False


def _is_interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _default_auth_method(headless: bool) -> str:
    return "device" if headless else "browser"


def _prompt_for_auth_method(headless: bool) -> str:
    default_method = _default_auth_method(headless)
    default_choice = "4" if default_method == "device" else "3"
    default_label = "device-code" if default_method == "device" else "浏览器登录"

    print("\n" + "=" * 60)
    print("  请选择登录方式")
    print("=" * 60)
    print("  3. 浏览器登录 (Browser Login Flow)")
    print("  4. 设备授权码登录 (Device Auth Flow)")
    if headless:
        print("  当前看起来是无图形环境，推荐选择 4。")
    print(f"  直接回车将默认使用: {default_label}")

    while True:
        choice = input(f"请选择 [3/4，默认 {default_choice}]: ").strip().lower()
        if not choice:
            return default_method

        if choice in {"3", "browser", "b"}:
            return "browser"

        if choice in {"4", "device", "device-code", "device_code", "d"}:
            return "device"

        print("[!] 无效输入，请输入 3、4，或直接回车。")


def _coerce_interval(value):
    try:
        return max(1, int(str(value).strip()))
    except Exception:
        return 5


def _build_browser_context(playwright, headless: bool):
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(user_agent=USER_AGENT)
    page = context.new_page()
    Stealth().apply_stealth_sync(page)
    return browser, context, page


def _fetch_chatgpt_session_token(api_request):
    response = api_request.get(
        CHATGPT_SESSION_URL,
        headers={"User-Agent": USER_AGENT},
        timeout=10000,
        fail_on_status_code=False,
    )

    if response.status != 200:
        return None

    try:
        data = response.json()
    except Exception:
        return None

    token = data.get("accessToken")
    if _token_is_valid(token):
        return token
    return None


def _build_http_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=15.0,
        follow_redirects=True,
    )


def browser_login_flow():
    """打开浏览器，让用户完成 ChatGPT Web 登录并捕获 accessToken。"""
    print("\n" + "=" * 60)
    print("  浏览器登录启动 (Browser Login Flow)")
    print("=" * 60 + "\n")
    print("[*] 即将打开一个浏览器窗口，请在其中登录 ChatGPT。")
    print("[*] 登录完成后，程序会自动捕获 accessToken 并关闭浏览器。")

    try:
        with sync_playwright() as p:
            browser, context, page = _build_browser_context(p, headless=False)
            try:
                page.goto(CHATGPT_LOGIN_URL, timeout=15000)

                start_time = time.time()
                while True:
                    if time.time() - start_time > AUTH_TIMEOUT_SECONDS:
                        print("\n[!] 浏览器登录超时。")
                        return None

                    token = _fetch_chatgpt_session_token(context.request)
                    if token:
                        _persist_access_token(token, "browser")
                        print("\n[+] 已捕获 ChatGPT accessToken。")
                        return token

                    time.sleep(2)
            finally:
                browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            print("[*] 正在安装浏览器驱动...")
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
            return browser_login_flow()

        print(f"[!] 浏览器登录流程出错: {e}")
        return None


def _request_device_code(api_request):
    response = api_request.post(
        f"{AUTH_API_BASE_URL}/deviceauth/usercode",
        json={"client_id": CLIENT_ID},
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    if response.status_code != 200:
        detail = response.text[:500].strip()
        print(f"[!] 获取授权码失败 (HTTP {response.status_code}): {detail}")
        return None

    try:
        return response.json()
    except Exception:
        print("[!] 获取授权码失败：响应不是有效 JSON。")
        return None


def _exchange_authorization_code(api_request, authorization_code, code_verifier):
    response = api_request.post(
        f"{AUTH_BASE_URL}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": DEVICE_AUTH_CALLBACK_URL,
            "client_id": CLIENT_ID,
            "code_verifier": code_verifier,
        },
        headers={"User-Agent": USER_AGENT},
    )

    if response.status_code != 200:
        detail = response.text[:500].strip()
        print(f"[!] OAuth Token 交换失败 (HTTP {response.status_code}): {detail}")
        return None

    try:
        return response.json().get("access_token")
    except Exception:
        return None


def device_login_flow():
    """纯 HTTP 的 device-code 登录流程，适合云主机和无界面环境。"""
    print("\n" + "=" * 60)
    print("  设备授权登录启动 (Device Auth Flow)")
    print("=" * 60 + "\n")
    print("[*] 此流程不依赖浏览器自动化，适合云主机 / SSH 环境。")

    try:
        with _build_http_client() as client:
            data = _request_device_code(client)
            if not data:
                return None

            user_code = data.get("user_code") or data.get("usercode")
            device_auth_id = data.get("device_auth_id")
            verification_uri = data.get("verification_uri", DEVICE_AUTH_URL)
            interval = _coerce_interval(data.get("interval", 5))

            if not user_code or not device_auth_id:
                print("[!] 设备授权响应缺少必要字段。")
                return None

            print(f"[*] 请在浏览器中打开链接：\n    👉 \033[1;34m{verification_uri}\033[0m")
            print(f"[*] 输入授权码：\n    🔑 \033[1;32m{user_code}\033[0m\n")
            print("[*] 正在等待授权...")

            start_time = time.time()
            while True:
                if time.time() - start_time > AUTH_TIMEOUT_SECONDS:
                    print("\n[!] 授权超时。")
                    return None

                response = client.post(
                    f"{AUTH_API_BASE_URL}/deviceauth/token",
                    json={
                        "device_auth_id": device_auth_id,
                        "user_code": user_code,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": USER_AGENT,
                    },
                )

                if response.status_code == 200:
                    try:
                        res_data = response.json()
                    except Exception:
                        res_data = {}

                    if "access_token" in res_data:
                        token = res_data["access_token"]
                        _persist_access_token(token, "device")
                        print("\n[+] 授权成功！")
                        return token

                    authorization_code = res_data.get("authorization_code")
                    code_verifier = res_data.get("code_verifier")
                    if authorization_code and code_verifier:
                        token = _exchange_authorization_code(
                            client,
                            authorization_code,
                            code_verifier,
                        )
                        if token:
                            _persist_access_token(token, "device")
                            print("\n[+] 授权成功！")
                            return token

                        print("\n[!] 捕获到授权码但交换失败。请重试。")
                        return None

                    print("\n[!] 授权响应缺少 access_token / authorization_code。")
                    return None

                if response.status_code in (400, 403, 404):
                    time.sleep(interval)
                    continue

                detail = response.text[:500].strip()
                print(f"\n[!] 轮询异常 (HTTP {response.status_code}): {detail}")
                return None
    except httpx.HTTPError as e:
        print(f"[!] Device-code HTTP 请求失败: {e}")
        return None
    except Exception as e:
        print(f"[!] Device-code 登录流程出错: {e}")
        return None


def ensure_authenticated(auth_method_override: str | None = None):
    token = get_access_token()
    if token:
        print("[*] 认证状态：已授权 (从 .env 加载)")
        return token

    token = _load_codex_access_token()
    if token:
        _persist_access_token(token, "codex")
        print("[*] 认证状态：已授权 (从 ~/.codex/auth.json 加载)")
        return token

    auth_method = _normalize_auth_method(auth_method_override) if auth_method_override else _get_auth_method()
    headless = _is_headless_environment()
    interactive = _is_interactive_terminal()
    attempted_device = False

    if auth_method == "prompt":
        if interactive:
            auth_method = _prompt_for_auth_method(headless)
        else:
            auth_method = _default_auth_method(headless)
            print(
                "[*] CHATGPT_AUTH_METHOD=prompt，但当前不是交互终端。"
                f" 自动改用 {auth_method}。"
            )
    elif auth_method == "auto" and interactive:
        auth_method = _prompt_for_auth_method(headless)

    if auth_method == "device":
        print("[*] 已配置 CHATGPT_AUTH_METHOD=device，直接进入 device-code 登录。")
        attempted_device = True
        token = device_login_flow()
        if token:
            return token
    elif auth_method == "browser":
        print("[*] 已配置 CHATGPT_AUTH_METHOD=browser，优先使用浏览器登录。")
        token = browser_login_flow()
        if token:
            return token
    elif headless:
        print("[*] 检测到无图形环境，跳过浏览器登录，直接进入 device-code 登录。")
        attempted_device = True
        token = device_login_flow()
        if token:
            return token
    else:
        print("未检测到有效的 ChatGPT Web 授权。正在打开浏览器登录...")
        token = browser_login_flow()
        if token:
            return token

    if not attempted_device:
        print("[*] 浏览器登录失败或不可用，尝试 device-code 兜底授权...")
        token = device_login_flow()
        if token:
            return token

    print("[!] 授权失败。程序将退出。")
    sys.exit(1)


if __name__ == "__main__":
    ensure_authenticated()
