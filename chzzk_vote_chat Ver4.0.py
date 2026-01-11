# chzzk_vote_chat_optimized.py — 1000명 규모 최적화 버전

import json
import threading
import requests
import random
import os
import time
import sys
import logging
from collections import deque

# -------------------------
# 의존성 확인
# -------------------------
try:
    import socketio  # python-socketio
except Exception as _imp_err:
    print("[치명적] python-socketio 클라이언트가 설치되지 않았습니다.")
    print("설치 명령: pip install python-socketio[client] requests")
    print("원인:", repr(_imp_err))
    input("엔터를 눌러 종료.")
    sys.exit(1)

# -------------------------
# 로깅 설정 (운영 환경 최적화)
# -------------------------
LOG_LEVEL = os.getenv("CHZZK_LOG", "WARNING").upper()  # INFO → WARNING
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.WARNING),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("chzzk")

# -------------------------
# 유틸: PyInstaller 경로
# -------------------------
def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath(os.path.dirname(__file__)), relative_path)

# ======== 경로/파일 상수 ========
EFFECT_NAMES_FILE = resource_path("모든 효과 이름.txt")
CONFIG_FILE = resource_path("config.json")
TOKEN_FILE = resource_path("access_token.json")

# ======== 저장 위치 설정 (UI 연동) ========
try:
    cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
    SAVE_DIR = cfg.get("save_dir", os.path.abspath(os.path.dirname(__file__)))
except Exception:
    SAVE_DIR = os.path.abspath(os.path.dirname(__file__))

# ======== 설정값 불러오기 & 예외 처리 ========
try:
    if not os.path.isdir(SAVE_DIR):
        logger.error("폴더가 없습니다: %s", SAVE_DIR)
        input("폴더 생성 또는 경로 설정 후 다시 실행하세요. 엔터로 종료.")
        sys.exit(1)

    with open(EFFECT_NAMES_FILE, "r", encoding="utf-8") as f:
        all_effects = [line.strip() for line in f if line.strip()]

    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)

    with open(TOKEN_FILE, encoding="utf-8") as f:
        token_data = json.load(f)

except Exception as e:
    logger.exception("[필수 파일 읽기/경로 오류]: %s", e)
    input("필수 파일이 없거나 잘못되었습니다. 엔터를 눌러 종료.")
    sys.exit(1)

# ======== 설정값 ========
CHANNEL_ID = config.get("channel_id")
ACCESS_TOKEN = token_data.get("accessToken")
VOTE_DURATION = int(config.get("vote_duration", 30))
RESULT_DURATION = int(config.get("result_duration", 60))
NEXT_VOTE_WAIT = int(config.get("vote_cooldown", 150))
RUNTIME = int(config.get("runtime", 3 * 60 * 60))
EFFECT_WEIGHTS = config.get("effect_weights", {})

# ======== CHZZK Open API 엔드포인트 ========
OPENAPI_BASE = "https://openapi.chzzk.naver.com"

# -------------------------
# REST 유틸 (표준 헤더 + 예외시 raise)
# -------------------------
def _std_headers(access_token: str | None = None):
    tok = access_token if access_token else ACCESS_TOKEN
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PythonRequests/2",
        "Origin": "https://chzzk.naver.com",
        "Referer": "https://chzzk.naver.com/",
    }

def http_get(path, params=None, timeout=10):
    url = f"{OPENAPI_BASE}{path}"
    r = requests.get(url, headers=_std_headers(), params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def http_post(path, params=None, json_body=None, timeout=10):
    url = f"{OPENAPI_BASE}{path}"
    r = requests.post(url, headers=_std_headers(), params=params, json=json_body, timeout=timeout)
    r.raise_for_status()
    return r.json() if r.content else None

# -------------------------
# 공지 전송 (공식 Chat API + 지수 백오프) - 재시도 횟수 감소
# -------------------------
def send_chat_notice(_channel_id_ignored: str, access_token: str, message: str):
    """
    공지 등록은 공식 Chat API를 사용합니다.
    Endpoint: POST /open/v1/chats/notice
    """
    path = "/open/v1/chats/notice"
    payload = {"message": message}
    s = requests.Session()
    backoff = 1
    for attempt in range(3):  # 5 → 3으로 감소
        try:
            url = f"{OPENAPI_BASE}{path}"
            logger.debug("[NOTICE] endpoint=%s", url)  # INFO → DEBUG
            r = s.post(url, headers=_std_headers(access_token), json=payload, timeout=10)
            r.raise_for_status()
            logger.info("[NOTICE] 공지 등록 성공")
            return
        except requests.HTTPError as he:
            status = he.response.status_code if he.response is not None else "N/A"
            logger.warning("공지 전송 오류 (HTTP %s, 재시도 %d)", status, attempt + 1)
        except Exception:
            logger.warning("공지 전송 오류 (재시도 %d)", attempt + 1)
        time.sleep(backoff)
        backoff = min(backoff * 2, 8)

# -------------------------
# 투표 결과 저장 (문자열 깨짐 방지)
# -------------------------
def save_vote_result_lua(effect_name):
    path = os.path.join(SAVE_DIR, "vote_result.lua")
    with open(path, "w", encoding="utf-8") as f:
        if effect_name is None or str(effect_name).strip().lower() == "none":
            f.write("")
            return
        f.write(f"effect_name={effect_name}\n")

def save_vote_result_txt(effect_name):
    path = os.path.join(SAVE_DIR, "vote_result.txt")
    with open(path, "w", encoding="utf-8") as f:
        if effect_name is None or str(effect_name).strip().lower() == "none":
            f.write("")
            return
        f.write(f"effect_name={effect_name}\n")

def save_vote_result_multi_lua(effect_names):
    """동표 결과만 저장 (0표는 제외, main.lua 호환)"""
    if not isinstance(effect_names, (list, tuple)) or len(effect_names) < 2:
        return
    path = os.path.join(SAVE_DIR, "vote_result.lua")
    with open(path, "w", encoding="utf-8") as f:
        joined = ", ".join(str(n) for n in effect_names)
        f.write(f"effect_name={joined}\n")

def save_vote_result_multi_txt(effect_names):
    """동표 결과만 저장 (0표는 제외, main.lua 호환)"""
    if not isinstance(effect_names, (list, tuple)) or len(effect_names) < 2:
        return
    path = os.path.join(SAVE_DIR, "vote_result.txt")
    with open(path, "w", encoding="utf-8") as f:
        joined = ", ".join(str(n) for n in effect_names)
        f.write(f"effect_name={joined}\n")
        
# -------------------------
# 세션 API (Socket.IO) 사용
# -------------------------
class ChzzkSessionListener:
    def __init__(self, access_token, on_chat_callback=None):
        self.access_token = access_token
        self.running = True
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
        self.session_key = None
        self.channel_id = None
        self._bind_handlers(on_chat_callback)

    def stop(self):
        try:
            self.running = False
            if self.sio.connected:
                self.sio.disconnect()
        except Exception as e:
            logger.warning("소켓 종료 중 오류: %s", e)

    @staticmethod
    def _asdict(payload):
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                return json.loads(payload)
            except Exception:
                return {"raw": payload}
        return {}

    def _bind_handlers(self, on_chat_callback):
        @self.sio.event
        def connect():
            logger.info("[SOCKET] 연결 성공")

        @self.sio.event
        def disconnect():
            logger.warning("[SOCKET] 연결 종료")

        @self.sio.on("SYSTEM")
        def on_system(data):
            try:
                d = ChzzkSessionListener._asdict(data)
                msg_type = d.get("type") or d.get("event") or d.get("raw")
                logger.debug("[SYSTEM] type=%s", msg_type)  # INFO → DEBUG
                if msg_type == "connected":
                    self.session_key = (d.get("data") or {}).get("sessionKey")
                    if not self.session_key:
                        logger.error("[SYSTEM] sessionKey 없음 - 구독 불가")
                        return
                    try:
                        http_post("/open/v1/sessions/events/subscribe/chat", params={"sessionKey": self.session_key})
                        logger.info("[SYSTEM] 채팅 이벤트 구독 완료")
                    except Exception:
                        logger.exception("[SYSTEM] 채팅 이벤트 구독 실패")

                elif msg_type == "subscribed":
                    di = (d.get("data") or {})
                    if di.get("eventType") == "CHAT":
                        self.channel_id = di.get("channelId")
                        logger.info("[SYSTEM] 구독 채널 ID: %s", self.channel_id)

            except Exception:
                logger.exception("[SYSTEM] 처리 중 오류")

        @self.sio.on("CHAT")
        def on_chat(data):
            try:
                d = ChzzkSessionListener._asdict(data)
                if on_chat_callback:
                    on_chat_callback(d)
            except Exception:
                logger.exception("[CHAT] 처리 중 오류")

        @self.sio.on("DONATION")
        def on_donation(data):
            logger.debug("[DONATION] %s", data)  # INFO → DEBUG

        @self.sio.on("SUBSCRIPTION")
        def on_subscription(data):
            logger.debug("[SUBSCRIPTION] %s", data)  # INFO → DEBUG

        @self.sio.event
        def connect_error(e):
            logger.error("[SOCKET] 연결 오류: %r", e)

    def create_session_url(self):
        try:
            resp = http_get("/open/v1/sessions/auth")
            content = resp.get("content") if isinstance(resp, dict) else None
            session_url = None
            if isinstance(content, dict):
                session_url = content.get("url")
            if not session_url and isinstance(resp, dict):
                session_url = resp.get("url")
            if not session_url:
                logger.error("세션 URL 응답 본문: %s", resp)
                raise RuntimeError("세션 URL이 응답에 없습니다")
            return session_url
        except Exception:
            logger.exception("세션 URL 발급 실패")
            raise

    def run_forever(self, headers=None):
        backoff = 2
        while self.running:
            try:
                url = self.create_session_url()
                logger.info("[SOCKET] 연결 시도: %s", url)
                self.sio.connect(
                    url,
                    transports=["websocket"],
                    wait_timeout=5,
                    headers=headers or {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Origin": "https://chzzk.naver.com",
                        "Referer": "https://chzzk.naver.com/",
                    },
                )
                self.sio.wait()
            except Exception:
                logger.exception("[SOCKET] 예외 발생 - 재시도 예정")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                try:
                    if self.sio.connected:
                        self.sio.disconnect()
                except Exception:
                    pass

# -------------------------
# ✅ 투표 로직 (Thread-Safe 개선)
# -------------------------
class VoteManager:
    def __init__(self, options):
        self.options = options
        self.votes = {opt: 0 for opt in options}
        self.user_voted_ids = set()
        self.voting = True
        self.lock = threading.Lock()  # 🔒 동시성 제어 추가
        
        # 📊 성능 모니터링용
        self.total_attempts = 0
        self.successful_votes = 0

    def chat_vote(self, user_id, vote):
        """Thread-safe 투표 처리"""
        with self.lock:  # 🔒 Critical Section
            self.total_attempts += 1
            if self.voting and vote in self.options and user_id not in self.user_voted_ids:
                self.votes[vote] += 1
                self.user_voted_ids.add(user_id)
                self.successful_votes += 1
                return True
            return False

    def end_vote(self):
        """단일 승자 반환"""
        with self.lock:
            self.voting = False
            max_votes = max(self.votes.values()) if self.votes else 0
            winners = [k for k, v in self.votes.items() if v == max_votes and v > 0]
            
            # 📊 투표 통계 로깅
            logger.info(
                "[투표 통계] 총 시도: %d, 성공: %d, 중복: %d",
                self.total_attempts,
                self.successful_votes,
                self.total_attempts - self.successful_votes
            )
            
            return winners[0] if winners else None

    def end_vote_multi(self):
        """동률(동표) 리스트 반환"""
        with self.lock:
            self.voting = False
            max_votes = max(self.votes.values()) if self.votes else 0
            if max_votes <= 0:
                return []
            return [k for k, v in self.votes.items() if v == max_votes]

    def get_current_votes(self):
        """현재 투표 현황 (Thread-safe 읽기)"""
        with self.lock:
            return dict(self.votes)

# ---- 가중치 기반 효과 3개 픽 (중복 방지) ----
def pick_effects_with_weight(all_effects, effect_weights, count=3):
    candidates, weights = [], []
    for e in all_effects:
        w = effect_weights.get(e, 10)
        if w > 0:
            candidates.append(e)
            weights.append(w)
    if len(candidates) <= count:
        base = candidates if candidates else all_effects
        k = min(count, len(base))
        return random.sample(base, k)
    selected = []
    pool = list(zip(candidates, weights))
    for _ in range(count):
        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        acc = 0.0
        idx = 0
        for i, (name, w) in enumerate(pool):
            acc += w
            if r <= acc:
                idx = i
                break
        selected.append(pool[idx][0])
        pool.pop(idx)
    return selected

# -------------------------
# 세션 리스너와 투표 매니저 연결
# -------------------------
def run_session_for_vote(vote_manager, vote_options):
    def generate_chat_handler(vote_manager, vote_options):
        def on_chat(data: dict):
            try:
                logger.debug("📥 [on_chat 수신됨]")  # INFO → DEBUG, 상세 로그 제거
                u = data or {}
                content = u.get("content", "")

                profile  = u.get("profile") or {}
                identity = u.get("identity") or {}
                sender   = u.get("sender") or {}

                voter_key = (
                    u.get("userIdHash")
                    or u.get("chatUserId")
                    or u.get("messageUserId")
                    or sender.get("userId")
                    or profile.get("userId")
                    or identity.get("userId")
                    or u.get("memberChannelId")
                    or u.get("senderChannelId")
                    or None
                )

                if not (content and voter_key):
                    return

                voter_key = str(voter_key)

                if vote_manager.voting and content.startswith("!투표"):
                    cmd = content[len("!투표"):].strip()
                    if cmd.isdigit():
                        idx = int(cmd) - 1
                        if 0 <= idx < len(vote_options):
                            success = vote_manager.chat_vote(voter_key, vote_options[idx])
                            if success:
                                logger.debug("🗳️ 투표 성공: %s → %s", voter_key, vote_options[idx])
                    elif cmd in vote_options:
                        success = vote_manager.chat_vote(voter_key, cmd)
                        if success:
                            logger.debug("🗳️ 투표 성공: %s → %s", voter_key, cmd)
            except Exception:
                logger.exception("on_chat 처리 오류")
        return on_chat

    on_chat_callback = generate_chat_handler(vote_manager, vote_options)
    listener = ChzzkSessionListener(ACCESS_TOKEN, on_chat_callback=on_chat_callback)
    t = threading.Thread(
        target=listener.run_forever,
        kwargs={"headers": {
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://chzzk.naver.com",
            "Referer": "https://chzzk.naver.com/",
        }},
        daemon=True,
    )
    t.start()

    time.sleep(1)

    return t, listener

# -------------------------
# 메시지 빌더 (문자열 안전 구성)
# -------------------------
def send_vote_status_notice(channel_id, access_token, options, votes, time_left):
    total = sum(votes.values())
    lines = []
    for idx, opt in enumerate(options, start=1):
        count = int(votes.get(opt, 0))
        percent = int((count / total) * 100) if total else 0
        lines.append(f"{idx}. {opt} {percent}% ({count}표)")
    msg = (
        f"[카오스 효과 투표 진행중] 남은 투표 가능시간: {time_left}초\n"
        + "\n".join(lines)
        + '\n채팅에 "!투표 1"처럼 입력해 투표 참여!'
    )
    send_chat_notice(channel_id, access_token, msg)

def build_start_msg(options, duration_sec):
    notice_lines = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
    return (
        f"[카오스 효과 투표 시작] 투표 가능시간: {duration_sec}초\n"
        f"{notice_lines}\n"
        '채팅에 "!투표 1"처럼 입력해 투표 참여!'
    )

def build_result_msg(options, votes, winner, result_duration):
    result_lines = "\n".join(f"{i}. {opt} {int(votes.get(opt, 0))}표" for i, opt in enumerate(options, start=1))
    return (
        f"[카오스 효과 투표 종료] 최다 득표 효과: {winner if winner is not None else '없음'}\n"
        f"{result_lines}\n"
        f"결과는 {result_duration}초 간 고정 유지됩니다."
    )

def _notice_channel_id(listener, fallback_id):
    """세션에서 받은 채널ID가 있으면 우선 사용, 없으면 config.channel_id"""
    if listener and getattr(listener, "channel_id", None):
        return listener.channel_id
    return fallback_id

# -------------------------
# 메인 루프
# -------------------------
def main():
    if RUNTIME <= 0:
        logger.error("RUNTIME 값이 0 이하입니다. config.json의 runtime을 확인하세요.")
        input("엔터를 눌러 종료.")
        return
    if VOTE_DURATION <= 0:
        logger.error("vote_duration 값이 0 이하입니다. config.json을 확인하세요.")
        input("엔터를 눌러 종료.")
        return
    if len(all_effects) < 3:
        logger.error("모든 효과 이름.txt 에 최소 3개 이상의 효과가 필요합니다.")
        input("엔터를 눌러 종료.")
        return

    start_time = time.time()
    round_count = 0
    
    while (time.time() - start_time) < RUNTIME:
        round_count += 1
        logger.info("=" * 50)
        logger.info("라운드 %d 시작", round_count)
        logger.info("=" * 50)
        
        options = pick_effects_with_weight(all_effects, EFFECT_WEIGHTS, count=3)
        duration = int(VOTE_DURATION)

        t_manager = VoteManager(options)
        t, listener = run_session_for_vote(t_manager, options)

        # 구독 채널ID 확보 대기
        for _ in range(40):  # 2초
            if getattr(listener, "channel_id", None):
                break
            time.sleep(0.05)
        notice_cid = _notice_channel_id(listener, CHANNEL_ID)

        # 시작 공지
        send_chat_notice(notice_cid, ACCESS_TOKEN, build_start_msg(options, duration))

        # 투표 진행
        for sec in range(duration, 0, -1):
            if sec == duration // 2:
                current_votes = t_manager.get_current_votes()
                send_vote_status_notice(notice_cid, ACCESS_TOKEN, options, current_votes, sec)
            time.sleep(1)

        # 마감 및 결과 저장/공지
        winner = t_manager.end_vote()
        save_vote_result_lua(winner)
        save_vote_result_txt(winner)

        winners = t_manager.end_vote_multi()
        if winners and len(winners) > 1:
            save_vote_result_multi_lua(winners)
            save_vote_result_multi_txt(winners)

        current_votes = t_manager.get_current_votes()
        send_chat_notice(notice_cid, ACCESS_TOKEN, build_result_msg(options, current_votes, winner, RESULT_DURATION))

        # 결과 고정 유지
        for _ in range(int(RESULT_DURATION)):
            time.sleep(1)

        # 📻 라운드 끝: 소켓/스레드 정리 (중요)
        try:
            listener.stop()
            t.join(timeout=5)
            del listener
            del t
            del t_manager
        except Exception:
            logger.exception("리소스 정리 중 예외")

        # 다음 라운드 대기
        wait_msg = f"[카오스 효과 투표] 다음 투표까지 {NEXT_VOTE_WAIT}초 대기 중."
        send_chat_notice(notice_cid, ACCESS_TOKEN, wait_msg)
        for _ in range(int(NEXT_VOTE_WAIT)):
            time.sleep(1)

    logger.info("=" * 50)
    logger.info("총 %d 라운드 완료 - 프로그램 종료", round_count)
    logger.info("=" * 50)
    input("엔터를 눌러 종료.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception("[예외 발생]: %s", e)
        input("오류가 발생했습니다. 엔터를 눌러 종료.")