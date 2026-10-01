# plaud-to-obsidian-llm-wiki

[Plaud](https://www.plaud.ai/) 녹음기에서 전사가 끝난 녹음을 Obsidian 볼트의 `raw/` 폴더에 Markdown 파일로 자동 저장하는 macOS 도구입니다.
[llm-wiki-template](https://github.com/tgYun783/llm-wiki-template)의 `raw/` 규칙(`YYYY-MM-DD_설명.md`, 원본 불변)에 맞춰 파일을 만듭니다. 저장된 녹음은 LLM 위키의 "자료 넣기"로 바로 정리하면 됩니다.

## 특징
- **전사가 끝난 녹음만 가져옵니다.** 아직 전사 중인 녹음은 다음 실행에서 다시 확인합니다.
- **AI 요약 노트까지 기다립니다.** raw 파일은 나중에 고치지 않으므로, 요약이 늦게 만들어지면 파일에 넣을 방법이 없습니다. 그래서 전사가 끝나도 Plaud AI 요약(`auto_sum_note`)이 생길 때까지 기다립니다. 전사 완료를 처음 확인한 시각부터 `PLAUD_NOTE_WAIT_HOURS`(기본 24시간)가 지나도 요약이 없으면 전사만 담아 내보내고, frontmatter에 `summary: none`, 본문 첫머리에 요약이 없다는 안내를 남깁니다.
- **파일명**: `YYYY-MM-DD_plaud-<제목>.md`. 날짜는 녹음한 날이고, 제목은 케밥케이스로 바꿉니다(한글 허용, 최대 40자).
- **내용**: Plaud AI 요약 노트와 전사 전문을 담습니다. 전사에는 타임스탬프와 화자가 붙고, frontmatter에는 `plaud_id`, 녹음 일시, 길이가 들어갑니다. 노트 본문에서 Plaud 앱에서만 열리는 링크와 이미지(`[...](plaud://...)`, `![...](plaud://...)`)는 Obsidian에서 열리지 않으므로 지웁니다. 링크만 있던 줄은 줄째 지우고, 다른 내용은 그대로 둡니다.
- **쓰기 전에 스스로 검사합니다.** MCP 서버는 늘 최신판을 받아 쓰기 때문에 응답 형식이 예고 없이 바뀔 수 있습니다. raw 파일은 한번 만들면 고치지 않으므로, 파일을 만들기 전에 아래 항목을 확인하고 하나라도 어긋나면 파일을 만들지 않습니다.
  - 녹음 시각(`start_at`, `start_time`, `created_at` 순서)을 해석할 수 있고 2015년 이후, 지금부터 하루 이내인지. `start_at`에 값이 있는데 해석되지 않으면 업로드 시각(`created_at`)으로 넘어가지 않고 실패로 봅니다. 해석하지 못하면 오늘 날짜로 대신하지 않습니다.
  - 전사 구간 개수가 `get_transcript`가 알려 준 `total`과 같은지. 페이지마다 `returned`, `offset`이 맞는지, `next_cursor`가 같은 값을 되풀이하지 않는지도 봅니다.
  - 전사 구간의 필드: 본문이 빈 구간이 절반을 넘지 않는지, `start_time`이 숫자인지(없는 구간은 절반까지 허용), `speaker`가 적어도 한 구간에는 있는지, 마지막 구간 시작이 녹음 길이보다 1분 넘게 늦지 않은지.
  - 요약: 요약 노트가 있으면 plaud 링크를 지운 본문이 비어 있지 않은지. `get_file`에는 요약이 있는데 `get_note`에서 사라졌는지.
  - 완성된 파일: frontmatter에 `plaud_id`와 `recorded_at`이 있는지, 전사 줄 수가 구간 수와 같은지, MCP 응답의 감싸개(`untrusted-user-data`)나 안내문(`Note: source_list`)이 섞이지 않았는지.
  - 그 밖에 `list_files` 결과에 `data` 목록이 있는지, `get_file` 결과가 JSON 객체인지도 봅니다.
- **기존 파일은 건드리지 않습니다.** 새 파일만 만들고, 이름이 겹치면 `-2`, `-3`처럼 번호를 붙입니다.
- **요약을 기다리는 녹음은 끝까지 챙깁니다.** 기본 실행은 최근 녹음 100건만 확인하지만, 요약을 기다리는 녹음은 그 범위 밖으로 밀려나도 따로 다시 확인합니다. Plaud에서 지운 녹음(404)이면 대기 기록을 지우고 로그에 남깁니다. 안전장치로, 대기 기록이 `PLAUD_NOTE_WAIT_HOURS`에 7일을 더한 시간보다 오래됐는데도 일시적이지 않은 오류로 계속 처리되지 않으면 대기 기록을 지웁니다. 지운 녹음은 `--all`이나 `--id`로 다시 받을 수 있습니다.
- **실패하면 다음 주기에 다시 시도합니다.** 네트워크 오류로 놓친 녹음도 다음 실행에서 가져옵니다. 상태 파일이 지워져도 raw 파일의 `plaud_id`를 보고 중복을 막습니다.
- **한 녹음이 계속 실패해도 전체가 멈추지 않습니다.** 노트 본문을 가져오지 못하면 보통은 다음 실행에서 다시 시도합니다. 링크 만료(HTTP 403, 404)처럼 일시적이지 않은 오류로 `PLAUD_MAX_RETRIES`번(기본 10번) 연속 실패하면 그 노트 없이 진행합니다. 빠진 노트가 AI 요약이면 요약이 없는 녹음과 똑같이 기다리다가 시간이 지나면 전사만 내보냅니다. 하이라이트처럼 다른 노트면 그 노트를 빼고 내보내고, 본문 첫머리에 빠진 노트 이름을 적습니다. 네트워크 끊김, 시간 초과, 서버 오류(5xx)는 횟수에 넣지 않고 계속 다시 시도합니다.
- **토큰을 직접 읽지 않습니다.** 공식 [Plaud MCP 서버](https://www.npmjs.com/package/@plaud-ai/mcp)를 stdio로 실행해 도구를 호출하고, 로그인과 토큰 갱신은 MCP 서버에 맡깁니다.
- **추가 패키지가 필요 없습니다.** Python 표준 라이브러리와 Node.js의 `npx`만 씁니다.

## 요구 사항
- macOS (자동 실행에 launchd 사용)
- Python 3.9 이상, Node.js 18 이상 (`npx`). 자동 실행은 macOS 기본 `/usr/bin/python3`을 씁니다.
- Plaud MCP 로그인 1회: Claude Code 같은 MCP 클라이언트에서 `@plaud-ai/mcp`의 `login` 도구를 실행합니다.

## 설치
```sh
git clone https://github.com/tgYun783/plaud-to-obsidian-llm-wiki.git
cd plaud-to-obsidian-llm-wiki

# 수동 실행용 (선택)
echo 'export PLAUD_RAW_DIR="$HOME/Obsidian/raw"' >> ~/.zshrc

# 자동 실행 등록: 로그인할 때 + 10분마다
PLAUD_RAW_DIR=~/Obsidian/raw ./install.sh
```

자동 실행을 끄려면 `./install.sh uninstall`을 실행합니다.

`install.sh`는 폴더와 환경변수 값을 모두 검사한 다음에 기존 자동 실행을 내리고 새로 등록합니다. 값이 잘못돼 설치가 멈춰도 이미 등록된 자동 실행은 그대로 동작합니다.

## 환경변수
| 이름 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `PLAUD_RAW_DIR` | ✅ | 없음 | 녹음 파일을 저장할 폴더 |
| `PLAUD_STATE_DIR` | | `~/Library/Application Support/plaud-sync` | `state.json`, `sync.log`, `launchd.err` 위치 |
| `PLAUD_SYNC_INTERVAL` | | `600` | 자동 실행 주기(초, 1 이상 정수). `install.sh`에서만 씁니다 |
| `PLAUD_NOTE_WAIT_HOURS` | | `24` | 전사가 끝난 뒤 AI 요약을 기다리는 최대 시간. `0`이면 기다리지 않습니다. `install.sh` 실행 때 지정하면 launchd 설정에도 들어갑니다 |
| `PLAUD_MAX_RETRIES` | | `10` | 노트 본문을 일시적이지 않은 오류로 몇 번 연속 못 가져오면 그 노트 없이 진행할지(1~1000). 10분 주기면 10번은 약 100분입니다. `install.sh` 실행 때 지정하면 launchd 설정에도 들어갑니다 |
| `PLAUD_MCP_CMD` | | `npx -y @plaud-ai/mcp@latest` | MCP 서버 실행 명령. 테스트에서 가짜 서버로 바꿀 때 씁니다 |
| `PLAUD_NO_NOTIFY` | | 없음 | `1`이면 macOS 알림을 띄우지 않습니다(테스트용) |

## 수동 실행
```sh
python3 plaud_sync.py                    # 최근 녹음 100건 확인
python3 plaud_sync.py --dry-run          # 가져올 대상만 표시
python3 plaud_sync.py --since 2026-10-01 # 이 날짜 이후 녹음
python3 plaud_sync.py --all              # 전체 다시 확인 (누락 복구)
python3 plaud_sync.py --id <ID> --force  # 특정 녹음을 요약을 기다리지 않고 새 파일로 받기
```

- `--force`는 `--id`와 함께만 씁니다. 이미 내보낸 녹음도 새 파일(`-2` 등)로 다시 만들고, AI 요약을 기다리지 않고 바로 내보냅니다.
- `--dry-run`은 파일도 상태(`state.json`)도 쓰지 않습니다. 요약을 기다리는 녹음은 "요약 대기"로 표시하고, 지울 대기 기록은 "대기 기록 삭제 예정"으로 표시합니다.
- `--id`로 실행하면 그 녹음만 처리하고, 목록 밖의 요약 대기 녹음은 다시 확인하지 않습니다.
- 로그인이 만료되면 macOS 알림이 한 번 뜹니다. 로그는 실행마다 남지만 알림은 다시 로그인할 때까지 반복하지 않습니다. MCP 클라이언트에서 다시 로그인하면 다음 주기부터 이어서 동작합니다.
- `--dry-run`도 전사와 노트를 받아 자체 검사까지 해 봅니다. 통과하면 "(자체 검사 통과)", 실패하면 "형식 검사 실패"로 표시합니다. 이때도 파일, 상태, 알림은 만들지 않습니다.
- 자체 검사는 `--id <ID> --force`로도 건너뛸 수 없습니다.

### 종료 코드
| 코드 | 뜻 |
|---|---|
| `0` | 정상 |
| `1` | 일부 녹음이 일시적인 오류(네트워크 등)로 실패. 다음 주기에 다시 시도합니다 |
| `2` | 설정 오류(`PLAUD_RAW_DIR` 없음 등) |
| `3` | Plaud 로그인 필요 |
| `4` | 자체 검사 실패(응답 형식 변경 의심). `1`과 함께 일어나면 `4`를 돌려줍니다 |

## 문제 해결
- **"Plaud 응답 형식이 바뀐 것 같아 저장을 멈췄어요" 알림**: 자체 검사에 걸려 파일을 만들지 않았다는 뜻입니다. 그 녹음은 내보낸 것으로 기록하지 않고 요약 대기 기록도 그대로 두므로, 원인이 풀리면 다음 실행에서 자동으로 내보냅니다. 같은 검사의 알림은 한 번만 뜨고, 그 검사가 다시 통과하면 기록을 지워 다음 실패 때 다시 알립니다. 로그는 실행마다 남습니다.
  1. `sync.log`에서 `형식 검사 실패(검사 이름) <녹음 ID>: <내용>` 줄을 찾습니다.
  2. 이 도구를 최신으로 업데이트합니다(`git pull`).
  3. 그래도 같은 실패가 나오면 로그 줄을 붙여 [이슈](https://github.com/tgYun783/plaud-to-obsidian-llm-wiki/issues)를 남겨 주세요. 로그에는 녹음 내용이 들어갈 수 있으니 붙이기 전에 확인합니다.
- **자동 실행에서 `npx`를 찾지 못함**: launchd는 `PATH`를 `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`으로만 실행합니다. Node.js를 nvm, volta, fnm 같은 버전 관리자로 설치했다면 `npx`가 이 경로에 없어서 `launchd.err`나 `sync.log`에 실패가 남습니다. `which node`로 위치를 확인한 뒤 둘 중 하나를 고릅니다.
  - `/usr/local/bin`에 링크를 만듭니다. 예: `sudo ln -s "$(which node)" /usr/local/bin/node`, `sudo ln -s "$(which npx)" /usr/local/bin/npx`
  - `install.sh`의 `PATH` 줄에 Node.js가 있는 폴더를 앞에 붙이고 `./install.sh`를 다시 실행합니다.
- **볼트가 `~/Documents`, `~/Desktop`, iCloud Drive에 있음**: macOS 개인정보 보호(TCC)가 launchd에서 실행한 `/usr/bin/python3`의 접근을 막을 수 있습니다. 수동 실행은 되는데 자동 실행만 `Operation not permitted`로 실패하면 이 경우입니다. 시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한에 `/usr/bin/python3`을 추가하거나(파일 선택 창에서 `Cmd+Shift+G`로 경로 입력), 볼트를 이 폴더들 밖으로 옮깁니다.

## 라이선스
MIT
