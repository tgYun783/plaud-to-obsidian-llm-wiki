# plaud → Obsidian raw 동기화

Plaud 녹음 중 전사가 끝난 것을 `$PLAUD_RAW_DIR/YYYY-MM-DD_plaud-<제목>.md`로 내보낸다.

- `plaud_sync.py` — 본체(표준 라이브러리만). Plaud MCP 서버(`npx @plaud-ai/mcp`)를 stdio로 띄워 도구를 호출한다. 토큰 파일은 직접 읽지 않는다.
- `install.sh` — launchd(`local.plaud-sync`) 등록. 환경변수를 plist에 기록한다. `./install.sh uninstall`로 제거.

## 환경변수
| 이름 | 필수 | 기본값 |
|---|---|---|
| `PLAUD_RAW_DIR` | ✅ | — |
| `PLAUD_STATE_DIR` | | `~/Library/Application Support/plaud-sync` (state.json, sync.log, launchd.err) |
| `PLAUD_SYNC_INTERVAL` | | `600` (install.sh 전용, 초) |

## 규칙
- raw에는 새 파일만 만든다(O_EXCL). 기존 파일은 덮어쓰거나 고치지 않는다.
- 날짜는 녹음 시각(로컬) 기준, 설명은 제목 케밥케이스(한글 허용, 최대 40자), `plaud-` 접두사.
- 실패한 녹음은 state에 남기지 않아 다음 실행에서 재시도. state가 없어도 raw 파일의 `plaud_id`로 중복을 막는다.
- 개인 경로·이름을 코드에 넣지 않는다 (공개 저장소).

## 수동 실행
```
python3 plaud_sync.py [--dry-run] [--since YYYY-MM-DD] [--all] [--id ID [--force]]
```
