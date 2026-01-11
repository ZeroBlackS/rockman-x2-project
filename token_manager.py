# 치지직 토큰 관리 통합 프로그램 (수동/자동 발급 통합)
import os
import sys
import json
import time
import tempfile
import threading
import webbrowser
import requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

# ================= 실행 경로/파일 경로 고정 =================
def get_app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR = get_app_dir()
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
TOKEN_FILE = os.path.join(APP_DIR, "access_token.json")
ERROR_FILE = os.path.join(APP_DIR, "access_token_error.txt")

print("[INFO] APP_DIR     =", APP_DIR)
print("[INFO] CONFIG_FILE =", CONFIG_FILE)
print("[INFO] TOKEN_FILE  =", TOKEN_FILE)

# 이벤트: 핸들러가 code를 받으면 set()
CODE_EVENT = threading.Event()

# ================= 파일 IO/진단 유틸 =================
def _atomic_json_write(path: str, data: dict):
    """임시 파일에 쓰고 os.replace로 교체 → 원자적 저장 + 타임스탬프 갱신"""
    d = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=d, prefix=".tmp_token_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
    now = time.time()
    os.utime(path, (now, now))

def _diagnose_write(folder: str) -> bool:
    """저장 폴더 쓰기 권한/경로 진단"""
    try:
        test_path = os.path.join(folder, ".write_test.tmp")
        with open(test_path, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(test_path)
        print("[OK] 폴더 쓰기 권한 확인:", folder)
        return True
    except Exception as e:
        print("❌ 폴더 쓰기 실패:", folder, "->", e)
        return False

# ================= 설정/토큰 헬퍼 =================
def get_config():
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"config.json이 없습니다: {CONFIG_FILE}")
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_token_dual(token_obj: dict):
    """
    기존 코드와 호환: top-level와 content에 동일 구조 저장.
    추가로 obtained_at / expiresIn 보강. 원자적 저장 + mtime 갱신.
    """
    flat = dict(token_obj)
    data = flat.copy()
    data["content"] = flat.copy()

    # 만료 메타데이터 보강
    obtained_at = int(time.time())
    expires_in = (
        data.get("expiresIn")
        or data.get("expires_in")
        or data["content"].get("expiresIn")
        or data["content"].get("expires_in")
        or 3600
    )
    data["obtained_at"] = obtained_at
    data["expiresIn"] = int(expires_in)

    _atomic_json_write(TOKEN_FILE, data)
    print(f"✅ access_token.json 저장 완료! -> {TOKEN_FILE}")
    print("[INFO] mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(TOKEN_FILE))))

def load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, encoding="utf-8") as f:
        return json.load(f)

def extract_access_refresh(token: dict):
    c = token.get("content", token)
    return c.get("accessToken"), c.get("refreshToken")

# ================= 로컬 리디렉트 서버/핸들러 =================
class _CodeCatcher(BaseHTTPRequestHandler):
    code = None
    state = None

    def do_GET(self):
        parsed = urlparse(self.path)

        # 1) favicon은 무시(204)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        qs = parse_qs(parsed.query)
        code = qs.get("code", [None])[0]
        state = qs.get("state", [None])[0]

        # 2) code가 있을 때만 저장 + 이벤트 set
        if code:
            _CodeCatcher.code = code
            _CodeCatcher.state = state
            print("[INFO] OAuth code captured:", code[:6] + "...", "state=", state)
            CODE_EVENT.set()

        # 3) 응답
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>인증이 완료되었습니다. 이 창은 닫으셔도 됩니다.</h2>".encode("utf-8"))

    def log_message(self, format, *args):
        # 불필요한 로그 출력 방지
        pass

def _run_code_server_by_redirect(redirect_uri: str):
    """
    config의 redirect_uri에서 host/port 추출해 그대로 바인딩.
    localhost/127.0.0.1/포트 변경 모두 자동 대응.
    """
    parsed = urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    server = HTTPServer((host, port), _CodeCatcher)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"[INFO] Redirect server listening on {host}:{port}")
    return server

# ================= OAuth 요청 =================
def _build_auth_url(cfg, state):
    base = "https://chzzk.naver.com/account-interlock"
    params = {
        "clientId": cfg["client_id"],
        "redirectUri": cfg.get("redirect_uri", "http://localhost:8080"),
        "state": state,
        "scope": cfg.get("scope", "chat:read chat:write chat:notice user:read"),
        "response_type": "code",
    }
    return f"{base}?{urlencode(params)}"

# ================= 1. 수동 토큰 발급 =================
def issue_token_manual():
    """
    수동 발급 플로우:
    - 브라우저 오픈
    - 사용자가 직접 code를 복사해서 입력
    - 토큰 교환 후 저장
    """
    config = get_config()
    client_id = config["client_id"]
    client_secret = config["client_secret"]
    redirect_uri = config.get("redirect_uri", "http://localhost:8080")
    state = config.get("state", "xyz123")
    scope = config.get("scope", "chat:read chat:write chat:notice user:read")

    params = {
        "clientId": client_id,
        "redirectUri": redirect_uri,
        "state": state,
        "scope": scope,
        "response_type": "code"
    }
    url = "https://chzzk.naver.com/account-interlock?" + urlencode(params)
    print("\n아래 주소를 브라우저에 붙여넣거나, 엔터로 자동 실행하세요:\n", url)
    input("엔터를 누르면 브라우저가 열립니다.")
    webbrowser.open(url)

    print("\n치지직 로그인, 권한동의 → 인증 후 주소창에 code=***&state=xyz123로 이동합니다.")
    code = input("\ncode 값을 붙여넣으세요: ").strip()

    token_url = "https://openapi.chzzk.naver.com/auth/v1/token"
    headers = {"Content-Type": "application/json"}
    data = {
        "grantType": "authorization_code",
        "clientId": client_id,
        "clientSecret": client_secret,
        "code": code,
        "state": state,
        "redirectUri": redirect_uri
    }
    print("토큰 발급 요청 중...")
    try:
        res = requests.post(token_url, headers=headers, json=data, timeout=20)
        if res.status_code == 200:
            res_content = res.json()
            token_obj = res_content.get("content", res_content)
            save_token_dual(token_obj)
            print("✅ 수동 발급 완료!")
        else:
            print("❌ 토큰 발급 실패!", res.status_code)
            print(res.text)
    except Exception as e:
        print("❌ 예외 발생:", e)

# ================= 2. 자동 토큰 발급 =================
def issue_token_auto():
    """
    자동 발급 플로우:
    - 브라우저 자동 오픈
    - 로컬서버에서 code/state 자동 캡처(Event)
    - 토큰 교환 후 access_token.json 저장
    """
    cfg = get_config()
    state = cfg.get("state", "xyz123")
    redirect_uri = cfg.get("redirect_uri", "http://localhost:8080")

    # 저장 폴더 쓰기 가능 여부 사전 점검
    if not _diagnose_write(APP_DIR):
        print("→ 관리자 권한으로 실행하거나, 다른 위치에서 실행해 주세요.")
        return

    # 이전 실행에서 남아있을 수 있는 값/이벤트 초기화
    _CodeCatcher.code = None
    _CodeCatcher.state = None
    CODE_EVENT.clear()

    server = _run_code_server_by_redirect(redirect_uri)

    auth_url = _build_auth_url(cfg, state)
    print("\n브라우저를 자동으로 엽니다. 로그인/동의 후 자동으로 코드가 수집됩니다.")
    print(auth_url)
    webbrowser.open(auth_url)

    print("code 수신 대기 중...(최대 10분)")
    got = CODE_EVENT.wait(timeout=600)  # 이벤트로 대기
    try:
        server.shutdown()
    except Exception:
        pass

    if not got or not _CodeCatcher.code:
        with open(ERROR_FILE, "w", encoding="utf-8") as f:
            f.write("code 수신 실패. redirectUri/방화벽/포트 점검 필요.\n")
            f.write(f"redirect_uri={redirect_uri}\n")
        print("❌ 코드를 받지 못했습니다. 자세한 내용:", ERROR_FILE)
        return

    print("코드 수신 완료. 토큰 교환 중...")
    masked = _CodeCatcher.code[:6] + "..." if _CodeCatcher.code else "(없음)"
    print("[INFO] received code =", masked, " state =", _CodeCatcher.state)

    url = "https://openapi.chzzk.naver.com/auth/v1/token"
    headers = {"Content-Type": "application/json"}
    data = {
        "grantType": "authorization_code",
        "clientId": cfg["client_id"],
        "clientSecret": cfg["client_secret"],
        "code": _CodeCatcher.code,
        "state": state,
        "redirectUri": redirect_uri,
    }

    try:
        res = requests.post(url, headers=headers, json=data, timeout=20)
        if res.status_code != 200:
            with open(ERROR_FILE, "w", encoding="utf-8") as f:
                f.write(f"HTTP {res.status_code}\n")
                f.write(res.text)
            print("❌ 토큰 교환 실패! 자세한 응답을 저장했습니다:", ERROR_FILE)
            print("→ 흔한 원인: code 재사용, redirect_uri 불일치, client_secret 오타")
            return

        body = res.json()
        token_obj = body.get("content", body)
        if not token_obj.get("accessToken"):
            with open(ERROR_FILE, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
            print("❌ 응답에 accessToken이 없습니다. 원문을 저장했습니다:", ERROR_FILE)
            return

        save_token_dual(token_obj)
        print("✅ 자동 발급 완료!")

    except Exception as e:
        with open(ERROR_FILE, "w", encoding="utf-8") as f:
            f.write("예외 발생: " + repr(e) + "\n")
        print("❌ 예외로 실패:", e, "| 자세한 내용:", ERROR_FILE)

# ================= 3. 토큰 갱신 =================
def refresh_token():
    token_data = load_token()
    if not token_data:
        print("❌ access_token.json이 없습니다. 먼저 토큰을 발급하세요(메뉴 1 또는 2).")
        return

    _, refresh_token_val = extract_access_refresh(token_data)
    if not refresh_token_val:
        print("❌ refreshToken 정보가 없습니다. 다시 발급하세요(메뉴 1 또는 2).")
        return

    cfg = get_config()
    url = "https://openapi.chzzk.naver.com/auth/v1/token"
    headers = {"Content-Type": "application/json"}
    data = {
        "grantType": "refresh_token",
        "clientId": cfg["client_id"],
        "clientSecret": cfg["client_secret"],
        "refreshToken": refresh_token_val
    }
    print("토큰 갱신 요청 중...")
    try:
        res = requests.post(url, headers=headers, json=data, timeout=20)
        if res.status_code == 200:
            body = res.json()
            token_obj = body.get("content", body)
            if not token_obj.get("accessToken"):
                with open(ERROR_FILE, "w", encoding="utf-8") as f:
                    json.dump(body, f, ensure_ascii=False, indent=2)
                print("❌ 응답에 accessToken이 없습니다. 원문을 저장했습니다:", ERROR_FILE)
                return
            save_token_dual(token_obj)
            print("✅ 토큰 갱신 완료!")
        else:
            with open(ERROR_FILE, "w", encoding="utf-8") as f:
                f.write(f"HTTP {res.status_code}\n")
                f.write(res.text)
            print("❌ 토큰 갱신 실패! 자세한 응답을 저장했습니다:", ERROR_FILE)
    except Exception as e:
        print("❌ 예외 발생:", e)

# ================= 4. 토큰 삭제 =================
def delete_token():
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)
        print("✅ access_token.json 삭제 완료!")
    else:
        print("access_token.json 파일이 없습니다.")

# ================= 메뉴 =================
def menu():
    print("\n====== 치지직 토큰 관리 (통합) ======")
    print("1. 토큰 발급 (수동 - code 직접 입력)")
    print("2. 토큰 발급 (자동 - 브라우저 자동 수집)")
    print("3. 토큰 갱신 (refresh_token)")
    print("4. 토큰 삭제")
    print("5. 종료")
    print("====================================")

# ================= 메인 실행 =================
if __name__ == "__main__":
    while True:
        try:
            menu()
            choice = input("원하는 작업의 번호를 입력하세요: ").strip()
            if choice == "1":
                issue_token_manual()
            elif choice == "2":
                issue_token_auto()
            elif choice == "3":
                refresh_token()
            elif choice == "4":
                delete_token()
            elif choice == "5":
                print("프로그램을 종료합니다.")
                break
            else:
                print("잘못된 입력입니다. 1~5 중에서 선택하세요.")
        except KeyboardInterrupt:
            print("\n프로그램을 종료합니다.")
            break
        except Exception as e:
            print("❌ 실행 중 예외:", e)
            # Windows 더블클릭 보호용 일시 정지
            if os.name == "nt":
                try:
                    input("\n계속하려면 엔터를 누르세요...")
                except Exception:
                    pass
    
    # 프로그램 종료 전 대기 (더블클릭 실행 시 창이 바로 닫히지 않도록)
    if os.name == "nt":
        try:
            input("\n종료하려면 엔터를 누르세요...")
        except Exception:
            pass