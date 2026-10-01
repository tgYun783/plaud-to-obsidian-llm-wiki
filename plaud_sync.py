#!/usr/bin/env python3
"""Plaud 전사 완료 녹음을 Obsidian 볼트의 raw/ 폴더(PLAUD_RAW_DIR)로 내보낸다.

환경변수:
  PLAUD_RAW_DIR    (필수) 내보낼 폴더. 예: ~/Obsidian/raw
  PLAUD_STATE_DIR  (선택) state.json, sync.log 위치. 기본 ~/Library/Application Support/plaud-sync
  PLAUD_NOTE_WAIT_HOURS (선택) 전사가 끝난 뒤 AI 요약을 기다릴 최대 시간. 기본 24
  PLAUD_MAX_RETRIES (선택) 노트 본문 가져오기가 연속으로 몇 번 실패하면 그 노트 없이 진행할지. 기본 10
  PLAUD_MCP_CMD   (선택, 테스트용) MCP 서버 실행 명령. 기본 "npx -y @plaud-ai/mcp@latest"
  PLAUD_NO_NOTIFY  (선택, 테스트용) 1이면 macOS 알림을 띄우지 않는다

- 인증은 Plaud MCP 서버(@plaud-ai/mcp)에 맡긴다. 이 스크립트는 토큰을 직접 읽지 않고,
  서버를 stdio로 띄워 MCP 도구(list_files / get_file / get_note / get_transcript)를 호출한다.
- 전사(transaction 블록)가 끝난 녹음만 내보낸다. 아직이면 다음 실행에서 다시 본다.
- raw 파일은 나중에 고칠 수 없으므로 AI 요약 노트도 기다린다. 전사 완료를 처음 본 시각을
  state의 pending에 적어 두고, PLAUD_NOTE_WAIT_HOURS가 지나도 요약이 없으면 전사만 내보낸다
  (frontmatter `summary: none`). --id X --force는 기다리지 않고 바로 내보낸다.
- raw/ 규칙: 새 파일만 만들고 기존 파일은 절대 덮어쓰거나 고치지 않는다.
  파일명은 `YYYY-MM-DD_plaud-<설명>.md` (날짜 = 녹음 날짜, 로컬 시간대).
- 실패(네트워크 등)한 녹음은 state의 exported에 기록하지 않으므로 다음 실행에서 자동 재시도된다.
- 요약을 기다리는 녹음(pending)은 최근 목록에서 밀려나도 get_file로 다시 확인한다. Plaud에서 지워진
  녹음(404)이면 대기 기록을 지운다. 대기 기록이 PLAUD_NOTE_WAIT_HOURS + 7일보다 오래됐는데도
  일시적이지 않은 오류로 계속 처리되지 않으면 대기 기록을 지운다(안전장치).
- 노트 본문(data_content_error)을 일시적이지 않은 오류로 연속 PLAUD_MAX_RETRIES번 못 가져오면
  그 녹음 때문에 실행 전체를 실패로 끝내지 않는다. 요약 노트면 요약이 없는 것으로 보고 대기/시간 초과
  흐름을 따르고, 다른 노트면 그 노트 없이 내보내며 본문에 빠진 노트를 적는다.
  실패 횟수는 state["failures"][id]에 적고, 내보내거나 노트를 가져오면 지운다.
- MCP 서버는 항상 최신판(@latest)이라 응답 형식이 예고 없이 바뀔 수 있다. 그래서 파일을 쓰기 전에
  자체 검사(녹음 시각, 전사 개수, 전사 필드, 요약 본문, 완성된 파일)를 하고, 하나라도 어긋나면
  쓰지 않는다(FormatCheckError). 그 녹음은 exported에 넣지 않고 pending도 그대로 두며, 검사마다
  알림은 한 번만 띄운다(state["format_alerts"]). 종료 코드는 4. --force로도 건너뛸 수 없다.

사용법:
  python3 plaud_sync.py                 # 최근 녹음 확인 후 새로 전사된 것만 내보내기
  python3 plaud_sync.py --dry-run       # 무엇을 내보낼지 보기만
  python3 plaud_sync.py --since 2026-10-01
  python3 plaud_sync.py --id <file_id>  # 특정 녹음만 (이미 내보냈거나 요약을 기다리지 않으려면 --force)
  python3 plaud_sync.py --all           # 전체 녹음 다시 훑기 (누락 복구)
"""
import argparse
import datetime as dt
import errno
import fcntl
import json
import os
import re
import select
import shlex
import subprocess
import sys
import time
import unicodedata

RAW_DIR = os.path.expanduser(os.environ.get("PLAUD_RAW_DIR", ""))
HERE = os.path.expanduser(os.environ.get(
    "PLAUD_STATE_DIR", "~/Library/Application Support/plaud-sync"))
STATE_PATH = os.path.join(HERE, "state.json")
LOCK_PATH = os.path.join(HERE, ".sync.lock")
LOG_PATH = os.path.join(HERE, "sync.log")
MCP_CMD = shlex.split(os.environ.get("PLAUD_MCP_CMD") or "npx -y @plaud-ai/mcp@latest")
RECENT_PAGES = 2          # 기본 실행 때 훑는 페이지 수 (페이지당 50건)
PAGE_SIZE = 50
CALL_TIMEOUT = 120        # 도구 호출 하나당 최대 대기(초)
SLUG_MAX = 40
DEFAULT_NOTE_WAIT_HOURS = 24
DEFAULT_MAX_RETRIES = 10  # 노트 본문 가져오기 연속 실패 허용 횟수(10분 주기면 약 100분)
EXIT_FORMAT = 4  # 종료 코드: 0 정상, 1 일시적 실패, 2 설정 오류, 3 로그인 필요, 4 형식 검사 실패
PENDING_GRACE = dt.timedelta(days=7)  # 요약 대기 시간에 더해, 처리 못 한 대기 기록을 지우기까지의 여유
# 네트워크, 시간 초과, 서버 오류(5xx), 요청 과다(429)는 일시적인 오류로 보고 실패 횟수에 넣지 않는다.
TRANSIENT_RE = re.compile(
    r"fetch failed|network|socket|ECONN|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|DNS|time(d)? ?out|abort"
    r"|\b5\d\d\b|\b429\b|too many requests|service unavailable|bad gateway", re.I)
# @plaud-ai/mcp는 HTTP 404를 "Failed to get file: Error: API error: 404 Not Found" 같은 isError 결과로 돌려준다.
NOT_FOUND_RE = re.compile(r"\b404\b|\bnot found\b", re.I)
# MCP 패키지 문서(skills/plaud-shared): note_list에서 data_type이 "auto_sum_note"인 항목이 AI 요약이다.
SUMMARY_NOTE_TYPE = "auto_sum_note"

UNTRUSTED_RE = re.compile(r"<(untrusted-user-data-[0-9a-f]+)[^>]*>\n(.*)\n</\1>", re.S)
# Plaud 앱에서만 열리는 링크/이미지(예: 요약 첫 줄의 [](plaud://image?type=summaryCard&id=...)). Obsidian에서는 깨진다.
# 링크 글자 안의 대괄호 한 겹, 주소 안의 괄호 한 겹, 뒤의 "제목"까지 링크 하나로 본다(일부만 지워 찌꺼기가 남지 않게).
PLAUD_LINK_RE = re.compile(
    r'!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(plaud://(?:[^()\s]|\([^()\s]*\))*(?:\s+"[^"\n]*")?\)')

# 자체 검사 기준값
MIN_RECORDED_YEAR = 2015          # 이보다 이른 녹음 시각은 단위나 형식이 바뀐 것으로 본다
FUTURE_SLACK = dt.timedelta(days=1)  # 녹음 시각이 지금보다 이만큼 넘게 미래면 이상으로 본다
MAX_EMPTY_RATIO = 0.5             # 내용이 빈 전사 구간이 이 비율을 넘으면(전부 빈 경우 포함) 형식 변경으로 본다
MAX_MISSING_START_RATIO = 0.5     # start_time이 없는 구간이 이 비율을 넘으면 형식 변경으로 본다
DURATION_SLACK_MS = 60000         # 마지막 구간 시작이 녹음 길이(duration)를 이만큼 넘게 지나면 단위 변경으로 본다
LEAK_MARKERS = ("untrusted-user-data", "Note: source_list")  # MCP 응답의 감싸개, 꼬리 안내문
TRANSCRIPT_HEADING = "## 전사"


def log(msg):
    line = "%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg):
    """macOS 알림. 실패해도 무시. PLAUD_NO_NOTIFY=1이면 띄우지 않는다."""
    if os.environ.get("PLAUD_NO_NOTIFY") == "1":
        return
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "Plaud → 위키"' % msg.replace('"', "'")],
                       timeout=10, capture_output=True)
    except Exception:
        pass


class AuthError(Exception):
    pass


class NotFoundError(RuntimeError):
    """도구가 isError로 404(녹음 없음)를 돌려줌. 네트워크 등 일시적 오류와는 구분한다."""


class FormatCheckError(Exception):
    """자체 검사 실패. 일시적 오류가 아니라 API 응답 형식이 바뀐 것으로 본다(파일을 쓰지 않음)."""

    def __init__(self, check, detail):
        Exception.__init__(self, "%s: %s" % (check, detail))
        self.check = check
        self.detail = detail


class McpClient:
    """의존성 없는 최소 MCP stdio 클라이언트."""

    def __init__(self):
        env = dict(os.environ)
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "/usr/bin:/bin")
        self.proc = subprocess.Popen(MCP_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
        self.next_id = 0
        self.buf = b""
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "plaud-wiki-sync", "version": "1.0"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _readline(self, deadline):
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError("MCP 서버 응답 시간 초과")
            r, _, _ = select.select([fd], [], [], remaining)
            if r:
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise ConnectionError("MCP 서버가 종료됨")
                self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line

    def _request(self, method, params):
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + CALL_TIMEOUT
        while True:
            line = self._readline(deadline).strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:  # JSON-RPC가 아닌 출력은 무시
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError("MCP 오류: %s" % msg["error"])
                return msg["result"]
            if "method" in msg and "id" in msg:  # 서버가 보낸 요청(roots/list 등)은 거절
                self._send({"jsonrpc": "2.0", "id": msg["id"],
                            "error": {"code": -32601, "message": "not supported"}})

    def call(self, tool, **args):
        res = self._request("tools/call", {"name": tool, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            # 인증 실패는 isError 결과로만 온다. 정상 결과 본문(녹음 내용)은 검사하지 않는다.
            if re.search(r"not authenticated|login|\b401\b|unauthorized", text, re.I):
                raise AuthError(text)
            if NOT_FOUND_RE.search(text) and not TRANSIENT_RE.search(text):
                raise NotFoundError("%s 실패(없음): %s" % (tool, text[:300]))
            raise RuntimeError("%s 실패: %s" % (tool, text[:300]))
        return text

    def call_json(self, tool, **args):
        text = self.call(tool, **args)
        m = UNTRUSTED_RE.search(text)
        payload = m.group(2) if m else text
        try:
            return json.loads(payload)
        except ValueError:
            return payload

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


# ---------- 상태 ----------

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"exported": {}}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def ids_already_in_raw():
    """state가 사라져도 중복 내보내기를 막기 위해 raw/의 plaud_id 표시를 읽는다."""
    found = {}
    for name in os.listdir(RAW_DIR):
        if "_plaud-" not in name or not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(RAW_DIR, name), encoding="utf-8") as f:
                head = f.read(2000)
        except OSError:
            continue
        m = re.search(r"^plaud_id:\s*\"?([^\"\s]+)", head, re.M)
        if m:
            found[m.group(1)] = name
    return found


# ---------- 변환 ----------

def parse_time(value):
    """API 시각(시간대 없으면 UTC) 또는 epoch(초/밀리초) → 로컬 datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, str) and re.fullmatch(r"\s*\d+(\.\d+)?\s*", value):
        value = float(value)
    if isinstance(value, (int, float)):
        secs = value / 1000.0 if value > 1e11 else float(value)
        try:
            return dt.datetime.fromtimestamp(secs)
        except (OverflowError, OSError, ValueError):  # 범위를 벗어난 값은 해석 실패로 본다
            return None
    s = str(value).strip().replace("Z", "+00:00").replace("z", "+00:00")
    # Python 3.9 fromisoformat은 소수초 3/6자리, 오프셋 +HH:MM만 받는다
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        d = dt.datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone().replace(tzinfo=None)


def recorded_time(file):
    """녹음 시작 시각. Plaud API는 start_at(UTC)에 준다. created_at은 업로드 시각이라 마지막 대안으로만 쓴다."""
    for key in ("start_at", "start_time", "created_at"):
        d = parse_time(file.get(key)) if isinstance(file, dict) else None
        if d:
            return d
    return None


def parse_since(value):
    """argparse용: YYYY-MM-DD → datetime (그날 0시, 로컬)."""
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError("날짜 형식은 YYYY-MM-DD 입니다: %s" % value)


def parse_wait_hours():
    raw = os.environ.get("PLAUD_NOTE_WAIT_HOURS", "").strip()
    if not raw:
        return float(DEFAULT_NOTE_WAIT_HOURS)
    try:
        hours = float(raw)
    except ValueError:
        hours = -1
    if not 0 <= hours <= 24 * 365:
        raise ValueError("PLAUD_NOTE_WAIT_HOURS는 0부터 8760 사이의 숫자(시간)여야 합니다: %s" % raw)
    return hours


def parse_max_retries():
    raw = os.environ.get("PLAUD_MAX_RETRIES", "").strip()
    if not raw:
        return DEFAULT_MAX_RETRIES
    if not re.fullmatch(r"\d+", raw) or not 1 <= int(raw) <= 1000:
        raise ValueError("PLAUD_MAX_RETRIES는 1부터 1000 사이의 정수여야 합니다: %s" % raw)
    return int(raw)


def slugify(name):
    s = unicodedata.normalize("NFC", name or "").lower()
    s = re.sub(r"\.(mp3|m4a|wav|opus|ogg)$", "", s)
    s = re.sub(r"[^\w가-힣]+", "-", s)     # 한글, 영숫자만 남기고 나머지는 -
    s = re.sub(r"_+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    s = s[:SLUG_MAX].rstrip("-")
    return s or "녹음"


def raw_filename(date, title):
    base = "%s_plaud-%s" % (date.strftime("%Y-%m-%d"), slugify(title))
    name = base + ".md"
    n = 2
    while os.path.exists(os.path.join(RAW_DIR, name)):
        name = "%s-%d.md" % (base, n)
        n += 1
    return name


def fmt_ts(ms):
    if not isinstance(ms, (int, float)):
        return ""
    total = int(ms / 1000)  # Plaud는 밀리초 단위
    return "%02d:%02d:%02d" % (total // 3600, total % 3600 // 60, total % 60)


def fmt_span(delta):
    return "%g시간" % round(delta.total_seconds() / 3600, 1)


def first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return None


def transcript_ready(file):
    for b in file.get("source_list") or []:
        if b.get("data_type") == "transaction" and (b.get("data_content") or b.get("data_link")):
            return True
    return False


def has_summary(notes):
    """노트 목록(get_file의 note_list 또는 get_note 결과)에 내용이 있는 AI 요약이 있는가.

    가정: data_type이 "auto_sum_note"이면 AI 요약이다(MCP 패키지 문서 기준). 하이라이트 노트처럼
    다른 종류가 먼저 생길 수 있어 그런 노트는 요약으로 치지 않는다. 다만 data_type이 아예 없는
    노트는 종류를 알 수 없으므로, 내용(data_content 또는 data_link)이 있으면 요약으로 본다.
    """
    for n in notes if isinstance(notes, list) else []:
        if not isinstance(n, dict) or not (n.get("data_content") or n.get("data_link")):
            continue
        if n.get("data_type") in (SUMMARY_NOTE_TYPE, None, ""):
            return True
    return False


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fetch_transcript(client, file_id):
    """전사 구간을 모든 페이지에서 모은다. 검사 2(transcript_count): 페이지마다 segments가 목록이고
    returned, offset이 실제와 맞는지, next_cursor가 반복되거나 빈 페이지에서 이어지지 않는지,
    끝에서 모은 개수가 total과 같은지 본다(각 필드는 응답에 있을 때만)."""
    segments, cursor, used, total = [], None, set(), None
    while True:
        args = {"file_id": file_id, "limit": 500}
        if cursor:
            args["cursor"] = cursor
        data = client.call_json("get_transcript", **args)
        if not isinstance(data, dict):
            raise FormatCheckError("transcript_count", "get_transcript 결과가 JSON 객체가 아님: %s" % str(data)[:200])
        page = data.get("segments")
        if not isinstance(page, list):
            raise FormatCheckError("transcript_count", "get_transcript 결과에 segments 목록이 없음")
        returned, offset = data.get("returned"), data.get("offset")
        if is_number(returned) and returned != len(page):
            raise FormatCheckError("transcript_count", "returned=%s인데 받은 구간은 %d개" % (returned, len(page)))
        if is_number(offset) and offset != len(segments):
            raise FormatCheckError("transcript_count", "offset=%s인데 앞서 받은 구간은 %d개" % (offset, len(segments)))
        if is_number(data.get("total")):
            if total is not None and data["total"] != total:
                raise FormatCheckError("transcript_count", "페이지마다 total이 다름(%s, %s)" % (total, data["total"]))
            total = data["total"]
        segments.extend(page)
        cursor = data.get("next_cursor")
        if not cursor:
            break
        if not page or cursor in used:
            raise FormatCheckError("transcript_count", "next_cursor가 끝나지 않음(빈 페이지이거나 같은 커서 반복)")
        used.add(cursor)
    if total is not None and len(segments) != total:
        raise FormatCheckError("transcript_count", "total=%s인데 모은 구간은 %d개" % (total, len(segments)))
    return segments


def segment_text(seg):
    """전사 구간 본문. 한 구간이 한 줄이 되도록 줄바꿈은 공백으로 바꾼다."""
    return re.sub(r"\s*\n\s*", " ", str(first(seg, "content", "text", "sentence") or ""))


def check_recorded_time(file):
    """검사 1(recorded_time): 녹음 시각을 해석할 수 있고 상식적인 범위에 있어야 한다. 오늘 날짜로 대신하지 않는다."""
    d = recorded_time(file)
    if d is None:
        raise FormatCheckError("recorded_time", "녹음 시각(start_at, start_time, created_at)을 해석할 수 없음: %s"
                               % {k: file.get(k) for k in ("start_at", "start_time", "created_at")})
    # 앞 순위 필드가 값은 있는데 해석되지 않으면(형식 변경) 업로드 시각 같은 뒤 순위 값으로 조용히 넘어가지 않는다
    for key in ("start_at", "start_time"):
        v = file.get(key)
        if v in (None, ""):
            continue
        if parse_time(v) is None:
            raise FormatCheckError("recorded_time", "%s 값을 해석할 수 없음: %r" % (key, v))
        break
    if d.year < MIN_RECORDED_YEAR or d > dt.datetime.now() + FUTURE_SLACK:
        raise FormatCheckError("recorded_time", "녹음 시각이 비정상 범위: %s" % d.isoformat(timespec="seconds"))
    return d


def check_segments(segments, file):
    """검사 3(segment_fields): 렌더링에 쓰는 필드가 그대로 있는지 본다.
    - 모든 구간이 객체(dict)여야 한다.
    - 본문이 빈 구간은 일부 허용하지만 MAX_EMPTY_RATIO(절반)를 넘으면 실패(전부 빈 경우 포함).
    - start_time은 있으면 숫자여야 하고(하나라도 문자열 등이면 실패), 없는 구간이 절반을 넘으면 실패.
    - speaker(또는 original_speaker)가 모든 구간에 없으면 실패.
    - get_file의 duration(밀리초)이 숫자면, 가장 늦은 start_time이 duration + 1분을 넘으면 실패(단위 변경)."""
    n = len(segments)
    if any(not isinstance(s, dict) for s in segments):
        raise FormatCheckError("segment_fields", "전사 구간 중 객체가 아닌 항목이 있음")
    empty = sum(1 for s in segments if not segment_text(s).strip())
    if empty > n * MAX_EMPTY_RATIO:
        raise FormatCheckError("segment_fields", "본문이 빈 전사 구간이 %d/%d개" % (empty, n))
    starts = [first(s, "start_time", "start", "begin") for s in segments]
    bad = [v for v in starts if v is not None and not is_number(v)]
    if bad:
        raise FormatCheckError("segment_fields", "start_time이 숫자가 아님: %r" % (bad[0],))
    missing = sum(1 for v in starts if v is None)
    if missing > n * MAX_MISSING_START_RATIO:
        raise FormatCheckError("segment_fields", "start_time이 없는 전사 구간이 %d/%d개" % (missing, n))
    if not any(first(s, "speaker", "original_speaker") for s in segments):
        raise FormatCheckError("segment_fields", "모든 전사 구간에 speaker가 없음")
    dur = file.get("duration")
    nums = [v for v in starts if v is not None]
    if is_number(dur) and dur > 0 and nums and max(nums) > dur + DURATION_SLACK_MS:
        raise FormatCheckError("segment_fields", "마지막 구간 시작(%s)이 녹음 길이(%s)보다 김(단위 변경 의심)"
                               % (max(nums), dur))


def strip_plaud_links(text):
    """Plaud 앱 전용 링크/이미지([..](plaud://..), ![..](plaud://..))만 지운다. 그 링크만 있던 줄은
    줄째 지우고, 그 때문에 빈 줄이 겹치거나 맨 앞에 남지 않게 한다. 다른 내용은 바꾸지 않는다."""
    out, dropped = [], False
    for line in str(text).split("\n"):
        new = PLAUD_LINK_RE.sub("", line)
        if new != line and not new.strip():
            dropped = True  # 링크만 있던 줄
            continue
        if dropped and not line.strip() and (not out or not out[-1].strip()):
            continue  # 지운 줄 때문에 생긴 연속 빈 줄, 맨 앞 빈 줄
        dropped = False
        out.append(new)
    return "\n".join(out)


def note_body(note):
    return strip_plaud_links(note.get("data_content") or "").strip()


def check_summary(file, notes, summary_lost):
    """검사 4(summary_body): 요약이 있다고 보는 노트(has_summary 기준)는 plaud:// 링크를 지운 본문이
    비어 있으면 안 된다. get_file의 note_list에는 요약이 있는데 get_note 결과에서 사라져도 실패.
    (본문 가져오기 실패가 한도에 닿아 요약을 뺀 경우 summary_lost는 제외)"""
    for n in notes:
        if is_summary_note(n) and (n.get("data_content") or n.get("data_link")) and not note_body(n):
            raise FormatCheckError("summary_body", "요약 노트(%s) 본문이 비어 있음"
                                   % (first(n, "data_title", "data_type") or "노트"))
    if not summary_lost and has_summary(file.get("note_list")) and not has_summary(notes):
        raise FormatCheckError("summary_body", "get_file에는 요약이 있는데 get_note 결과에 없음")


def check_rendered(content, fid, n_segments):
    """검사 5(rendered_file): 쓰기 직전 완성된 파일을 본다. frontmatter의 plaud_id가 이 녹음이고
    recorded_at이 비어 있지 않은지, 전사 줄 수가 구간 수와 같은지, MCP 감싸개나 꼬리 안내문이
    섞이지 않았는지."""
    m = re.match(r"---\n(.*?)\n---\n", content, re.S)
    if not m:
        raise FormatCheckError("rendered_file", "frontmatter를 찾을 수 없음")
    fm = m.group(1)
    if not re.search(r'^plaud_id: "%s"$' % re.escape(fid), fm, re.M):
        raise FormatCheckError("rendered_file", "frontmatter의 plaud_id가 없거나 다름")
    if not re.search(r"^recorded_at: \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", fm, re.M):
        raise FormatCheckError("rendered_file", "frontmatter의 recorded_at이 비어 있거나 형식이 다름")
    parts = content.rsplit("\n%s\n" % TRANSCRIPT_HEADING, 1)
    if len(parts) != 2:
        raise FormatCheckError("rendered_file", "전사 제목(%s)이 없음" % TRANSCRIPT_HEADING)
    lines = [l for l in parts[1].split("\n") if l.strip()]
    if len(lines) != n_segments:
        raise FormatCheckError("rendered_file", "전사 줄 %d개, 구간 %d개" % (len(lines), n_segments))
    for marker in LEAK_MARKERS:
        if marker in content:
            raise FormatCheckError("rendered_file", "MCP 응답 문구(%s)가 본문에 섞임" % marker)


def render(file, notes, segments, no_summary_reason=None, missing_notes=None):
    """no_summary_reason: AI 요약 없이 내보낼 때 본문에 남길 설명(없으면 None).
    missing_notes: 본문을 끝내 가져오지 못해 빼고 내보내는 노트 제목 목록."""
    title = first(file, "name", "filename", "title") or "녹음"
    start = recorded_time(file)
    dur = first(file, "duration")
    lines = [
        "---",
        "source: plaud",
        'plaud_id: "%s"' % file["id"],
        'title: %s' % json.dumps(title, ensure_ascii=False),
        "recorded_at: %s" % (start.strftime("%Y-%m-%d %H:%M") if start else ""),
        "duration: %s" % (fmt_ts(dur) if dur else ""),
        "exported_at: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    ]
    if no_summary_reason:
        lines.append("summary: none")
    lines += ["---", "", "# %s" % title, ""]
    if no_summary_reason:
        lines += ["> Plaud AI 요약 없음: %s" % no_summary_reason, ""]
    if missing_notes:
        lines += ["> 가져오지 못한 Plaud 노트: %s (본문을 여러 번 받지 못해 빼고 넣었습니다)"
                  % ", ".join(missing_notes), ""]
    for note in notes if isinstance(notes, list) else []:
        body = note_body(note)
        if not body:
            continue
        head = first(note, "data_title", "title", "data_type") or "노트"
        lines += ["## Plaud 노트: %s" % head, "", body, ""]
    lines += [TRANSCRIPT_HEADING, ""]
    for seg in segments:
        text = segment_text(seg)
        speaker = first(seg, "speaker", "original_speaker") or ""
        ts = fmt_ts(first(seg, "start_time", "start", "begin"))
        prefix = "[%s] " % ts if ts else ""
        lines.append("%s**%s**: %s" % (prefix, speaker, text) if speaker else prefix + text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_new_file(name, content):
    """새 파일만 생성(O_EXCL). 임시 파일을 거쳐 원자적으로 만든다."""
    tmp = os.path.join(HERE, ".export.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    dest = os.path.join(RAW_DIR, name)
    try:
        os.link(tmp, dest)  # 대상이 이미 있으면 FileExistsError → 덮어쓰지 않음
    except OSError as e:
        # 다른 볼륨이거나 하드링크를 지원하지 않는 파일시스템이면 직접 생성 (EEXIST 등은 그대로 실패)
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
        except BaseException:
            os.remove(dest)  # 방금 만든 미완성 파일만 지운다 (plaud_id 때문에 중복 판정되는 것 방지)
            raise
    finally:
        os.remove(tmp)


# ---------- 메인 ----------

def list_candidates(client, args):
    if args.id:
        return [{"id": args.id}]
    pages = 10 ** 6 if args.all or args.since else RECENT_PAGES
    since = args.since
    out, seen_ids = [], set()
    for p in range(1, pages + 1):
        data = client.call_json("list_files", page=p, page_size=PAGE_SIZE)
        # 형식이 바뀌어 목록을 못 읽으면 "새 녹음 0건"으로 조용히 넘어가지 않도록 검사한다
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise FormatCheckError("list_format", "list_files 결과에 data 목록이 없음: %s" % str(data)[:200])
        items = data["data"]
        items = [it for it in items if isinstance(it, dict) and it.get("id")]
        if not items:  # 빈 페이지 = 끝
            break
        new = [it for it in items if it["id"] not in seen_ids]
        if not new:  # 같은 페이지가 되풀이되면(서버가 page를 무시하는 경우) 무한 반복 방지
            break
        stop = False
        for it in new:
            seen_ids.add(it["id"])
            # 목록은 업로드 시각(created_at) 순이다. 녹음 시각 <= 업로드 시각이므로, 업로드가 since보다
            # 이르면 그 뒤 항목도 대상이 아니라고 보고 멈춘다. 늦게 업로드된 옛 녹음 때문에 멈추지 않도록
            # 멈춤 판단은 created_at으로, 거르기는 녹음 시각으로 한다.
            uploaded = parse_time(it.get("created_at"))
            if since and uploaded and uploaded < since:
                stop = True
            recorded = recorded_time(it)
            if since and recorded and recorded < since:
                continue
            out.append(it)
        # 페이지 크기가 PAGE_SIZE보다 작아도 끝이라고 단정하지 않는다(서버가 상한을 둘 수 있음).
        # 응답에 total이 있으면 그것으로 끝을 판단한다.
        total = data.get("total")
        if stop or (isinstance(total, int) and len(seen_ids) >= total):
            break
    return out


def wait_status(pending, fid, now, wait, dry_run):
    """요약이 없을 때: (계속 기다릴지, 전사 완료를 처음 본 시각). dry-run이면 state를 바꾸지 않는다."""
    entry = pending.get(fid) if isinstance(pending.get(fid), dict) else {}
    seen = None
    try:
        seen = dt.datetime.fromisoformat(entry.get("transcript_seen", ""))
    except (TypeError, ValueError):
        pass
    if seen is None:
        seen = now
        if not dry_run:
            pending[fid] = {"transcript_seen": now.isoformat(timespec="seconds")}
    return now - seen < wait, seen


def pending_seen(pending, fid):
    """대기 기록의 전사 완료 확인 시각. 없거나 형식이 틀리면 None."""
    entry = pending.get(fid)
    try:
        return dt.datetime.fromisoformat(entry.get("transcript_seen", ""))
    except (AttributeError, TypeError, ValueError):
        return None


def is_summary_note(note):
    """has_summary와 같은 기준: data_type이 auto_sum_note이거나 비어 있으면 요약 노트로 본다."""
    return isinstance(note, dict) and note.get("data_type") in (SUMMARY_NOTE_TYPE, None, "")


def record_failure(failures, fid, error):
    """노트 본문 가져오기의 연속 실패 횟수를 1 늘리고 늘어난 값을 돌려준다."""
    entry = failures.get(fid) if isinstance(failures.get(fid), dict) else {}
    count = entry.get("count") if isinstance(entry.get("count"), int) else 0
    failures[fid] = {"count": count + 1,
                     "last": dt.datetime.now().isoformat(timespec="seconds"),
                     "error": error[:300]}
    return count + 1


def run(args):
    if not os.path.isdir(RAW_DIR):
        log("raw 폴더 없음: %s" % RAW_DIR)
        return 2
    state = load_state()
    exported = state.setdefault("exported", {})
    pending = state.setdefault("pending", {})
    failures = state.setdefault("failures", {})
    in_raw = ids_already_in_raw()
    wait = dt.timedelta(hours=args.wait_hours)
    pending_cap = wait + PENDING_GRACE

    def drop_pending(fid, why):
        """대기 기록(과 실패 횟수)을 지운다. dry-run이면 로그만 남긴다."""
        if args.dry_run:
            log("[dry-run] 대기 기록 삭제 예정: %s, %s" % (fid, why))
            return
        pending.pop(fid, None)
        failures.pop(fid, None)
        save_state(state)
        log("대기 기록 삭제: %s, %s" % (fid, why))

    def pending_too_old(fid):
        seen = pending_seen(pending, fid)
        return seen is not None and dt.datetime.now() - seen > pending_cap

    def save():
        if not args.dry_run:
            save_state(state)

    passed, format_failed = set(), {}  # 이번 실행에서 통과한 검사, 실패한 검사 → 첫 실패 설명

    client = McpClient()
    try:
        try:
            candidates = list_candidates(client, args)
        except FormatCheckError as e:
            log_format_failure(e, "list_files")
            update_format_alerts(state, passed, {e.check: "list_files: %s" % e.detail}, args.dry_run)
            return EXIT_FORMAT
        if not args.id:
            passed.add("list_format")
        dirty = False
        for book in (pending, failures):  # 이미 내보낸 녹음의 대기 기록, 실패 횟수 정리
            for fid in list(book):
                if fid in exported or fid in in_raw:
                    del book[fid]
                    dirty = True
        if dirty:
            save()
        todo = [c for c in candidates
                if args.force or (c["id"] not in exported and c["id"] not in in_raw)]
        # 요약을 기다리는 녹음이 최근 목록 밖으로 밀려났어도 get_file로 다시 확인한다 (--id 실행은 제외)
        rechecks = []
        if not args.id:
            listed = {c["id"] for c in candidates}
            rechecks = [{"id": fid} for fid in pending if fid not in listed]
        log("확인 %d건, 미내보냄 %d건%s" % (len(candidates), len(todo),
            ", 목록 밖 요약 대기 %d건 재확인" % len(rechecks) if rechecks else ""))
        todo += rechecks
        done = planned = waiting = summary_waiting = failed = 0
        authed = not args.id  # list_files가 성공했으면 로그인된 상태 (--id면 get_file 성공으로 판단)
        for item in todo:
            fid = item["id"]
            try:
                try:
                    file = client.call_json("get_file", file_id=fid)
                except NotFoundError:
                    if fid not in pending:
                        raise
                    drop_pending(fid, "Plaud에서 녹음을 찾을 수 없음(삭제된 것으로 봄)")
                    continue
                if not isinstance(file, dict):
                    raise FormatCheckError("get_file_format", "get_file 결과가 JSON 객체가 아님: %s" % str(file)[:200])
                passed.add("get_file_format")
                file.setdefault("id", fid)
                authed = True
                if not transcript_ready(file):
                    if fid in pending and pending_too_old(fid):
                        drop_pending(fid, "전사가 보이지 않는 채로 대기 기록이 %s를 넘김" % fmt_span(pending_cap))
                        continue
                    waiting += 1
                    continue
                title = first(file, "name", "filename", "title") or "녹음"
                # 검사 1: 녹음 시각. 해석하지 못하면 오늘 날짜로 대신하지 않고 쓰지 않는다.
                date = check_recorded_time(file)
                passed.add("recorded_time")
                if not has_summary(file.get("note_list")) and not args.force:
                    now = dt.datetime.now()
                    keep_waiting, seen = wait_status(pending, fid, now, wait, args.dry_run)
                    if keep_waiting:
                        summary_waiting += 1
                        if not args.dry_run:
                            save_state(state)
                        else:
                            log("[dry-run] 요약 대기: %s (전사 완료 확인 %s, 최대 %g시간)"
                                % (title, seen.strftime("%Y-%m-%d %H:%M"), args.wait_hours))
                        continue
                # dry-run도 여기부터는 실제 실행과 같이 전사와 노트를 받아 자체 검사까지 한다(파일, state는 쓰지 않음)
                segments = fetch_transcript(client, fid)  # 검사 2
                passed.add("transcript_count")
                if not segments:
                    waiting += 1
                    continue
                check_segments(segments, file)  # 검사 3
                passed.add("segment_fields")
                notes = client.call_json("get_note", file_id=fid)
                if not isinstance(notes, list):
                    if has_summary(file.get("note_list")):
                        raise FormatCheckError("summary_body", "get_note 결과가 목록이 아님: %s" % str(notes)[:200])
                    notes = []
                bad = [n for n in notes if isinstance(n, dict) and n.get("data_content_error")]
                missing, summary_lost = [], False
                if bad:
                    # raw는 고칠 수 없으니 빠진 채로 내보내지 않고 다시 시도한다. 일시적 오류는 횟수에 넣지 않는다.
                    errors = "; ".join(str(n["data_content_error"])[:200] for n in bad)
                    if any(TRANSIENT_RE.search(str(n["data_content_error"])) for n in bad):
                        raise RuntimeError("노트 본문을 가져오지 못함(일시적 오류): %s" % errors)
                    count = record_failure(failures, fid, errors)  # dry-run이면 메모리에서만 센다
                    save()
                    if count < args.max_retries:
                        raise RuntimeError("노트 본문을 가져오지 못함(연속 %d/%d번): %s"
                                           % (count, args.max_retries, errors))
                    # 연속 실패가 한도에 닿음: 이 녹음 때문에 실행 전체를 실패로 끝내지 않는다
                    notes = [n for n in notes if n not in bad]
                    missing = [first(n, "data_title", "title", "data_type") or "노트"
                               for n in bad if not is_summary_note(n)]
                    summary_lost = any(is_summary_note(n) for n in bad)
                    log("노트 본문을 연속 %d번 가져오지 못해 그 노트 없이 진행: %s (%s)" % (count, title, errors))
                elif failures.pop(fid, None) is not None:
                    save()  # 노트를 가져왔으니 실패 횟수 초기화
                notes = [n for n in notes if isinstance(n, dict)]
                check_summary(file, notes, summary_lost)  # 검사 4
                passed.add("summary_body")
                reason = None
                if not has_summary(notes):
                    if args.force:
                        reason = "--force로 요약을 기다리지 않고 내보냈습니다."
                    else:
                        keep_waiting, _ = wait_status(pending, fid, dt.datetime.now(), wait, args.dry_run)
                        if keep_waiting:  # 요약 본문을 못 가져온 경우도 요약이 없는 것으로 보고 기다린다
                            summary_waiting += 1
                            save()
                            if args.dry_run:
                                log("[dry-run] 요약 대기: %s (노트에 요약 없음)" % title)
                            continue
                        if summary_lost:
                            reason = ("AI 요약 노트 본문을 가져오지 못한 채 전사가 끝난 뒤 %g시간이 지나 전사만 넣었습니다."
                                      % args.wait_hours)
                        else:
                            reason = "전사가 끝난 뒤 %g시간 안에 요약이 만들어지지 않아 전사만 넣었습니다." % args.wait_hours
                name = raw_filename(date, title)
                content = render(file, notes, segments, reason, missing)
                check_rendered(content, fid, len(segments))  # 검사 5
                passed.add("rendered_file")
                if args.dry_run:
                    planned += 1
                    log("[dry-run]%s %s → %s (자체 검사 통과)" % (" (요약 없이)" if reason else "", title, name))
                    continue
                write_new_file(name, content)
                exported[fid] = {"file": name, "at": dt.datetime.now().isoformat(timespec="seconds")}
                pending.pop(fid, None)
                failures.pop(fid, None)
                save_state(state)
                log("내보냄%s%s: %s → raw/%s" % (" (요약 없음)" if reason else "",
                                              " (빠진 노트 %d개)" % len(missing) if missing else "", title, name))
                done += 1
            except AuthError:
                raise
            except FormatCheckError as e:
                # 형식 변경 의심: 쓰지 않고, exported에 넣지 않고, 대기 기록도 지우지 않는다(안전장치 삭제도 하지 않음)
                format_failed.setdefault(e.check, "%s: %s" % (fid, e.detail))
                log_format_failure(e, fid)
            except Exception as e:  # 한 건 실패해도 나머지는 계속, 다음 실행에서 재시도
                # OSError(MCP 서버 종료, 응답 시간 초과, 파이프 끊김, 로컬 파일 오류 등)도 녹음 문제가 아니므로 일시적으로 본다
                transient = isinstance(e, OSError) or TRANSIENT_RE.search(str(e))
                if fid in pending and pending_too_old(fid) and not transient:
                    # 안전장치: 요약 대기 시간 + 7일이 지나도록 일시적이지 않은 오류로 처리하지 못한 녹음
                    drop_pending(fid, "%s 넘게 처리하지 못함: %s" % (fmt_span(pending_cap), e))
                    continue
                failed += 1
                log("실패(다음 실행에 재시도) %s: %s" % (fid, e))
        if authed and state.pop("auth_notified_at", None) is not None and not args.dry_run:
            save_state(state)  # 다시 로그인됨 → 다음 만료 때 다시 알린다
        update_format_alerts(state, passed, format_failed, args.dry_run)
        n_format = len(format_failed)
        if args.dry_run:
            log("[dry-run] 완료: 내보낼 예정 %d건, 전사 대기 %d건, 요약 대기 %d건, 실패 %d건, 형식 검사 실패 %d종"
                % (planned, waiting, summary_waiting, failed, n_format))
        else:
            log("완료: 새로 %d건, 전사 대기 %d건, 요약 대기 %d건, 실패 %d건, 형식 검사 실패 %d종"
                % (done, waiting, summary_waiting, failed, n_format))
        if done:
            notify("새 녹음 %d건을 raw에 넣었어요" % done)
        if format_failed:
            return EXIT_FORMAT
        return 1 if failed else 0
    finally:
        client.close()


def log_format_failure(e, where):
    log("형식 검사 실패(%s) %s: %s. 파일을 만들지 않았습니다. Plaud MCP 응답 형식이 바뀐 것 같습니다. "
        "이 도구를 최신으로 업데이트하거나 이슈를 남겨 주세요" % (e.check, where, " ".join(str(e.detail).split())))


def update_format_alerts(state, passed, failed, dry_run):
    """형식 검사 알림은 검사 이름마다 한 번만 띄운다(state["format_alerts"][검사] = 처음 실패한 시각).
    어떤 실행에서 그 검사가 한 번 이상 통과하고 한 번도 실패하지 않으면 기록을 지워, 다음에 다시
    실패하면 또 알린다. 로그는 매번 남긴다. dry-run은 알림도 state 쓰기도 하지 않는다."""
    if dry_run:
        return
    alerts = state.get("format_alerts")
    alerts = alerts if isinstance(alerts, dict) else {}
    cleared = [c for c in alerts if c in passed and c not in failed]
    for c in cleared:
        del alerts[c]
        log("형식 검사 다시 통과: %s (알림 기록 해제)" % c)
    new = [c for c in failed if c not in alerts]
    now = dt.datetime.now().isoformat(timespec="seconds")
    for c in new:
        alerts[c] = now
    if new:
        notify("Plaud 응답 형식이 바뀐 것 같아 저장을 멈췄어요(%s). sync.log를 확인하세요" % ", ".join(new))
    if cleared or new:
        if alerts:
            state["format_alerts"] = alerts
        else:
            state.pop("format_alerts", None)
        save_state(state)


def notify_auth_once(dry_run):
    """로그아웃 1회당 알림 1번. 알린 시각을 state에 적고, 로그인된 실행이 성공하면 run()이 지운다."""
    if dry_run:
        return
    state = load_state()
    if state.get("auth_notified_at"):
        return
    notify("Plaud 로그인이 만료됐어요. Claude Code에서 다시 로그인하세요")
    state["auth_notified_at"] = dt.datetime.now().isoformat(timespec="seconds")
    save_state(state)


def main():
    ap = argparse.ArgumentParser(description="Plaud 전사 → 위키 raw/ 내보내기")
    ap.add_argument("--dry-run", action="store_true", help="파일과 state를 쓰지 않고 대상만 표시")
    ap.add_argument("--all", action="store_true", help="전체 녹음을 훑음")
    ap.add_argument("--since", type=parse_since, help="이 날짜(YYYY-MM-DD) 이후 녹음만 훑음")
    ap.add_argument("--id", help="특정 녹음 ID만 처리")
    ap.add_argument("--force", action="store_true",
                    help="--id와 함께: 이미 내보낸 녹음도 새 파일로 다시 내보내고, 요약을 기다리지 않음")
    args = ap.parse_args()
    if args.force and not args.id:
        ap.error("--force는 --id와 함께만 쓸 수 있습니다")
    try:
        args.wait_hours = parse_wait_hours()
        args.max_retries = parse_max_retries()
    except ValueError as e:
        ap.error(str(e))

    if not RAW_DIR:
        print("PLAUD_RAW_DIR 환경변수를 설정하세요 (예: export PLAUD_RAW_DIR=~/Obsidian/raw)", file=sys.stderr)
        return 2
    os.makedirs(HERE, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("다른 동기화가 실행 중이라 건너뜀")
        return 0
    try:
        return run(args)
    except AuthError:
        log("Plaud 로그인 필요: Plaud MCP의 login 도구로 다시 로그인하세요")
        try:
            notify_auth_once(args.dry_run)
        except OSError as e:
            log("알림 상태 저장 실패: %s" % e)
        return 3
    except Exception as e:  # 네트워크 끊김 등: 다음 주기에 자동 재시도
        log("동기화 실패(다음 주기에 재시도): %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
