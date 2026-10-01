# plaud → Obsidian raw 동기화

Plaud 녹음 중 전사가 끝난 것을 `$PLAUD_RAW_DIR/YYYY-MM-DD_plaud-<제목>.md`로 내보낸다.

- `plaud_sync.py`: 본체(표준 라이브러리만). Plaud MCP 서버(`npx -y @plaud-ai/mcp@latest`)를 stdio로 띄워 도구를 호출한다. 토큰 파일은 직접 읽지 않는다.
- `install.sh`: launchd(`local.plaud-sync`) 등록. 환경변수를 plist에 기록한다. `./install.sh uninstall`로 제거한다. 설치할 때는 입력 검사(폴더, `PLAUD_NOTE_WAIT_HOURS`, `PLAUD_MAX_RETRIES`, `PLAUD_SYNC_INTERVAL`)와 plist 임시 파일 쓰기를 끝낸 뒤에야 `launchctl bootout`을 부른다. 설치가 실패해도 기존 에이전트는 살아 있다. uninstall은 바로 bootout한다.

## 환경변수
| 이름 | 필수 | 기본값 |
|---|---|---|
| `PLAUD_RAW_DIR` | ✅ | 없음 |
| `PLAUD_STATE_DIR` | | `~/Library/Application Support/plaud-sync` (state.json, sync.log, launchd.err) |
| `PLAUD_SYNC_INTERVAL` | | `600` (install.sh 전용, 초) |
| `PLAUD_NOTE_WAIT_HOURS` | | `24` (전사 완료 뒤 AI 요약을 기다리는 최대 시간, 0이면 안 기다림) |
| `PLAUD_MAX_RETRIES` | | `10` (노트 본문 가져오기의 연속 실패 허용 횟수, 1~1000) |
| `PLAUD_MCP_CMD` | | `npx -y @plaud-ai/mcp@latest` (테스트에서 가짜 MCP 서버로 바꿀 때) |
| `PLAUD_NO_NOTIFY` | | 없음 (`1`이면 알림 끔, 테스트용) |

## 규칙
- raw에는 새 파일만 만든다(`os.link`, 다른 볼륨이면 O_EXCL). 기존 파일은 덮어쓰거나 고치지 않는다.
- 날짜는 녹음 시각(로컬) 기준, 설명은 제목 케밥케이스(한글 허용, 최대 40자), `plaud-` 접두사.
- 실패한 녹음은 `state["exported"]`에 남기지 않아 다음 실행에서 재시도한다. state가 없어도 raw 파일의 `plaud_id`로 중복을 막는다.
- 전사가 끝나도 AI 요약(get_file `note_list`의 `data_type == "auto_sum_note"`, data_type이 없는 노트는 내용이 있으면 요약으로 간주)이 생길 때까지 기다린다. 전사 완료를 처음 본 시각은 `state["pending"][id]["transcript_seen"]`에 적고, 내보내면 지운다. `PLAUD_NOTE_WAIT_HOURS`가 지나면 전사만 내보내고 frontmatter에 `summary: none`을 넣는다. 로그에서는 "요약 대기"로 "전사 대기"와 따로 센다.
- `--id`가 아닌 실행은 `pending`에 있지만 이번에 훑은 목록에 없는 녹음도 get_file로 다시 확인한다. get_file이 isError로 404(`\b404\b` 또는 `not found`, 네트워크성 문구가 없을 때)를 돌려주면 `NotFoundError`로 보고 대기 기록을 지운다. 네트워크, 인증 오류와는 구분한다(인증은 `AuthError`가 먼저). 안전장치: `transcript_seen`이 `PLAUD_NOTE_WAIT_HOURS + 7일`보다 오래된 대기 기록은, 이번 처리가 일시적이지 않은 오류로 실패하거나 전사가 보이지 않으면 지운다. 일시적 오류(`TRANSIENT_RE`: 네트워크, 시간 초과, 5xx, 429)나 `OSError`(MCP 서버 종료, 응답 시간 초과, 로컬 파일 오류)일 때는 지우지 않는다.
- get_note 결과에 `data_content_error`가 있으면 기본은 실패(다음 실행에 재시도)다. 일시적 오류가 하나라도 섞이면 횟수에 넣지 않는다. 일시적이지 않은 오류만 있으면 `state["failures"][id] = {"count", "last", "error"}`의 count를 1 늘린다. count가 `PLAUD_MAX_RETRIES`에 닿으면 그 노트를 빼고 진행해 실행 실패로 세지 않는다. 빠진 노트가 요약이면 요약 없음으로 보고 대기/시간 초과 흐름을 탄다. 다른 노트면 빼고 내보내며 본문에 `> 가져오지 못한 Plaud 노트: ...` 줄을 넣는다. 노트를 다 가져오거나 내보내면 count를 지운다. 일시적 오류는 count를 늘리지도 지우지도 않는다.
- `--force`는 `--id`와 함께만 쓴다. 이미 내보낸 녹음도 새 파일로 만들고 요약 대기를 건너뛴다. `--dry-run`은 state를 쓰지 않는다(대기 기록 삭제도 로그로만 알린다).
- 로그인 만료 알림은 로그아웃 1회당 한 번(`state["auth_notified_at"]`). 로그인된 실행이 성공하면 지운다. 로그는 매번 남긴다.
- 노트 본문의 `[...](plaud://...)`, `![...](plaud://...)`는 `strip_plaud_links`로 지운다. 링크만 있던 줄은 줄째 지우고, 그 때문에 생긴 연속 빈 줄과 맨 앞 빈 줄만 정리한다. 다른 내용은 바꾸지 않는다. 전사 구간 본문의 줄바꿈은 공백으로 바꿔 한 구간이 한 줄이 되게 한다(줄 수 검사 때문).
- 자체 검사(`FormatCheckError(check, detail)`): 파일을 쓰기 전에 아래를 확인하고 어긋나면 쓰지 않는다. 일시적 오류가 아니라 응답 형식 변경으로 본다.
  - `recorded_time`(`check_recorded_time`): `start_at`, `start_time`, `created_at` 순으로 해석. 실패하거나 2015년 이전, 지금+1일 이후면 실패. 앞 순위 필드에 값이 있는데 해석되지 않아도 실패(뒤 순위로 넘어가지 않음). 오늘 날짜로 대신하지 않는다.
  - `transcript_count`(`fetch_transcript`): 페이지마다 `segments`가 목록, `returned == len(segments)`, `offset == 앞서 받은 개수`, `total`이 페이지마다 같음, `next_cursor`가 빈 페이지에서 이어지거나 반복되지 않음, 끝에서 모은 개수 `== total`. 필드가 응답에 없으면 그 항목은 건너뛴다.
  - `segment_fields`(`check_segments`): 모든 구간이 dict, 빈 본문이 절반 이하, `start_time`은 있으면 숫자이고 없는 구간이 절반 이하, `speaker`가 한 구간 이상, 최대 `start_time <= duration + 60000`.
  - `summary_body`(`check_summary`): 요약 노트(`is_summary_note`이고 content나 link가 있음)의 plaud 링크 제거 후 본문이 비면 실패. get_file `note_list`에 요약이 있는데 get_note 결과에 없거나 목록이 아니면 실패(본문 가져오기 실패 한도로 요약을 뺀 `summary_lost`는 제외).
  - `rendered_file`(`check_rendered`): frontmatter의 `plaud_id`가 이 녹음, `recorded_at`이 `YYYY-MM-DD HH:MM`, 마지막 `## 전사` 아래 비지 않은 줄 수 `==` 구간 수, `untrusted-user-data`와 `Note: source_list` 문자열이 없음.
  - `list_format`(list_files에 `data` 목록), `get_file_format`(get_file이 dict).
- 자체 검사 실패 처리: 그 녹음은 파일을 쓰지 않고 `exported`에 넣지 않으며 `pending`도 그대로 둔다(안전장치 삭제도 하지 않음). 로그는 매번 `형식 검사 실패(<검사>) <id>: <내용>`으로 남긴다. 알림은 검사 이름마다 한 번(`state["format_alerts"][검사] = 처음 실패 시각`). 어떤 실행에서 그 검사가 한 번 이상 통과하고 한 번도 실패하지 않으면 기록을 지운다. `--dry-run`은 검사까지 하되 알림과 state 쓰기는 하지 않는다. `--force`로도 건너뛸 수 없다.
- 종료 코드: 0 정상, 1 일시적 실패, 2 설정 오류, 3 로그인 필요, 4 자체 검사 실패(1보다 우선).
- 개인 경로나 이름을 코드에 넣지 않는다 (공개 저장소).

## 수동 실행
```
python3 plaud_sync.py [--dry-run] [--since YYYY-MM-DD] [--all] [--id ID [--force]]
```
