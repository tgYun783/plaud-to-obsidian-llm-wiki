#!/usr/bin/env python3
"""Plaud 전사 완료 녹음을 Obsidian 볼트의 raw/ 폴더(PLAUD_RAW_DIR)로 내보낸다.

환경변수:
  PLAUD_RAW_DIR    (필수) 내보낼 폴더. 예: ~/Obsidian/raw
  PLAUD_STATE_DIR  (선택) state.json·sync.log 위치. 기본 ~/Library/Application Support/plaud-sync

- 인증은 Plaud MCP 서버(@plaud-ai/mcp)에 맡긴다. 이 스크립트는 토큰을 직접 읽지 않고,
  서버를 stdio로 띄워 MCP 도구(list_files / get_file / get_note / get_transcript)를 호출한다.
- 전사(transaction 블록)가 끝난 녹음만 내보낸다. 아직이면 다음 실행에서 다시 본다.
- raw/ 규칙: 새 파일만 만들고 기존 파일은 절대 덮어쓰거나 고치지 않는다.
  파일명은 `YYYY-MM-DD_plaud-<설명>.md` (날짜 = 녹음 날짜, 로컬 시간대).
- 실패(네트워크 등)한 녹음은 state에 기록하지 않으므로 다음 실행에서 자동 재시도된다.

사용법:
  python3 plaud_sync.py                 # 최근 녹음 확인 후 새로 전사된 것만 내보내기
  python3 plaud_sync.py --dry-run       # 무엇을 내보낼지 보기만
  python3 plaud_sync.py --since 2026-10-01
  python3 plaud_sync.py --id <file_id>  # 특정 녹음만 (이미 내보냈으면 --force)
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
MCP_CMD = ["npx", "-y", "@plaud-ai/mcp@latest"]
RECENT_PAGES = 2          # 기본 실행 때 훑는 페이지 수 (페이지당 50건)
PAGE_SIZE = 50
CALL_TIMEOUT = 120        # 도구 호출 하나당 최대 대기(초)
SLUG_MAX = 40

UNTRUSTED_RE = re.compile(r"<(untrusted-user-data-[0-9a-f]+)[^>]*>\n(.*)\n</\1>", re.S)


def log(msg):
    line = "%s %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg):
    """macOS 알림. 실패해도 무시."""
    try:
        subprocess.run(["osascript", "-e", 'display notification "%s" with title "Plaud → 위키"' % msg.replace('"', "'")],
                       timeout=10, capture_output=True)
    except Exception:
        pass


class AuthError(Exception):
    pass


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
            msg = json.loads(line)
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
            if re.search(r"not authenticated|login", text, re.I):
                raise AuthError(text)
            raise RuntimeError("%s 실패: %s" % (tool, text[:300]))
        if re.search(r"Not authenticated", text):
            raise AuthError(text)
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
    if isinstance(value, (int, float)):
        secs = value / 1000.0 if value > 1e11 else float(value)
        return dt.datetime.fromtimestamp(secs)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s.replace(" ", "T"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone().replace(tzinfo=None)


def slugify(name):
    s = unicodedata.normalize("NFC", name or "").lower()
    s = re.sub(r"\.(mp3|m4a|wav|opus|ogg)$", "", s)
    s = re.sub(r"[^\w가-힣]+", "-", s)     # 한글·영숫자만 남기고 나머지는 -
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


def fetch_transcript(client, file_id):
    segments, cursor = [], None
    while True:
        args = {"file_id": file_id, "limit": 500}
        if cursor:
            args["cursor"] = cursor
        data = client.call_json("get_transcript", **args)
        if not isinstance(data, dict):
            raise RuntimeError("전사 형식을 알 수 없음: %s" % str(data)[:200])
        segments.extend(data.get("segments") or [])
        cursor = data.get("next_cursor")
        if not cursor:
            return segments


def render(file, notes, segments):
    title = first(file, "name", "filename", "title") or "녹음"
    start = parse_time(first(file, "start_time", "created_at"))
    dur = first(file, "duration")
    lines = [
        "---",
        "source: plaud",
        'plaud_id: "%s"' % file["id"],
        'title: %s' % json.dumps(title, ensure_ascii=False),
        "recorded_at: %s" % (start.strftime("%Y-%m-%d %H:%M") if start else ""),
        "duration: %s" % (fmt_ts(dur) if dur else ""),
        "exported_at: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "---",
        "",
        "# %s" % title,
        "",
    ]
    for note in notes if isinstance(notes, list) else []:
        body = note.get("data_content")
        if not body:
            continue
        head = first(note, "data_title", "title", "data_type") or "노트"
        lines += ["## Plaud 노트 — %s" % head, "", str(body).strip(), ""]
    lines += ["## 전사", ""]
    for seg in segments:
        text = first(seg, "content", "text", "sentence") or ""
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
        if e.errno != errno.EXDEV:  # 상태 폴더와 raw가 다른 볼륨이면 직접 생성
            raise
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    finally:
        os.remove(tmp)


# ---------- 메인 ----------

def list_candidates(client, args):
    if args.id:
        return [{"id": args.id}]
    pages = 10 ** 6 if args.all or args.since else RECENT_PAGES
    since = dt.datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    out = []
    for p in range(1, pages + 1):
        data = client.call_json("list_files", page=p, page_size=PAGE_SIZE)
        items = data.get("data", []) if isinstance(data, dict) else []
        stop = False
        for it in items:
            created = parse_time(first(it, "start_time", "created_at"))
            if since and created and created < since:
                stop = True
                continue
            out.append(it)
        if len(items) < PAGE_SIZE or stop:
            break
    return out


def run(args):
    if not os.path.isdir(RAW_DIR):
        log("raw 폴더 없음: %s" % RAW_DIR)
        return 2
    state = load_state()
    exported = state.setdefault("exported", {})
    in_raw = ids_already_in_raw()

    client = McpClient()
    try:
        candidates = list_candidates(client, args)
        todo = [c for c in candidates
                if args.force or (c["id"] not in exported and c["id"] not in in_raw)]
        log("확인 %d건, 미내보냄 %d건" % (len(candidates), len(todo)))
        done = waiting = failed = 0
        for item in todo:
            fid = item["id"]
            try:
                file = client.call_json("get_file", file_id=fid)
                if not isinstance(file, dict):
                    raise RuntimeError("get_file 형식 오류")
                file.setdefault("id", fid)
                if not transcript_ready(file):
                    waiting += 1
                    continue
                title = first(file, "name", "filename", "title") or "녹음"
                date = parse_time(first(file, "start_time", "created_at")) or dt.datetime.now()
                if args.dry_run:
                    log("[dry-run] %s → %s" % (title, raw_filename(date, title)))
                    continue
                segments = fetch_transcript(client, fid)
                if not segments:
                    waiting += 1
                    continue
                notes = client.call_json("get_note", file_id=fid)
                name = raw_filename(date, title)
                write_new_file(name, render(file, notes, segments))
                exported[fid] = {"file": name, "at": dt.datetime.now().isoformat(timespec="seconds")}
                save_state(state)
                log("내보냄: %s → raw/%s" % (title, name))
                done += 1
            except AuthError:
                raise
            except Exception as e:  # 한 건 실패해도 나머지는 계속, 다음 실행에서 재시도
                failed += 1
                log("실패(다음 실행에 재시도): %s — %s" % (fid, e))
        log("완료: 새로 %d건, 전사 대기 %d건, 실패 %d건" % (done, waiting, failed))
        if done:
            notify("새 녹음 %d건을 raw에 넣었어요" % done)
        return 1 if failed else 0
    finally:
        client.close()


def main():
    ap = argparse.ArgumentParser(description="Plaud 전사 → 위키 raw/ 내보내기")
    ap.add_argument("--dry-run", action="store_true", help="파일을 만들지 않고 대상만 표시")
    ap.add_argument("--all", action="store_true", help="전체 녹음을 훑음")
    ap.add_argument("--since", help="이 날짜(YYYY-MM-DD) 이후 녹음만 훑음")
    ap.add_argument("--id", help="특정 녹음 ID만 처리")
    ap.add_argument("--force", action="store_true", help="이미 내보낸 녹음도 새 파일로 다시 내보냄")
    args = ap.parse_args()

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
        log("Plaud 로그인 필요 — Plaud MCP의 login 도구로 다시 로그인하세요")
        notify("Plaud 로그인이 만료됐어요. Claude Code에서 다시 로그인하세요")
        return 3
    except Exception as e:  # 네트워크 끊김 등: 다음 주기에 자동 재시도
        log("동기화 실패(다음 주기에 재시도): %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
