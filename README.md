# plaud-to-obsidian-llm-wiki

[Plaud](https://www.plaud.ai/) 녹음기의 **전사가 끝난 녹음**을 Obsidian 볼트의 `raw/` 폴더로 자동으로 내보내는 macOS 도구입니다.
[llm-wiki-template](https://github.com/tgYun783/llm-wiki-template)의 `raw/` 규칙(`YYYY-MM-DD_설명.md`, 원본 불변)에 맞춰 파일을 만들기 때문에, 내보낸 녹음은 LLM 위키의 "자료 넣기"로 바로 정리할 수 있습니다.

## 특징
- **전사 완료 녹음만** 내보냅니다. 전사 중인 녹음은 다음 실행에서 다시 확인합니다.
- **파일명**: `YYYY-MM-DD_plaud-<제목>.md` (날짜 = 녹음 날짜, 제목은 케밥케이스, 한글 허용)
- **내용**: Plaud AI 요약 노트 + 타임스탬프·화자가 붙은 전사 전문. frontmatter에 `plaud_id`, 녹음 일시, 길이.
- **raw 불변**: 새 파일만 만들고 기존 파일은 절대 덮어쓰지 않습니다. 이름이 겹치면 `-2`를 붙입니다.
- **자동 재시도**: 네트워크 오류 등으로 실패한 녹음은 다음 주기에 다시 시도합니다. 상태 파일이 없어져도 `plaud_id`로 중복을 막습니다.
- **토큰을 직접 다루지 않음**: 공식 [Plaud MCP 서버](https://www.npmjs.com/package/@plaud-ai/mcp)를 stdio로 띄워 도구를 호출하므로 로그인·토큰 갱신은 MCP 서버가 맡습니다.
- 의존성 없음 (Python 3.9+ 표준 라이브러리, Node.js의 `npx`).

## 요구 사항
- macOS (자동 실행은 launchd)
- Python 3.9+, Node.js 18+ (`npx`)
- Plaud MCP 로그인 1회 — Claude Code 등 MCP 클라이언트에서 `@plaud-ai/mcp`의 `login` 도구를 실행

## 설치
```sh
git clone https://github.com/tgYun783/plaud-to-obsidian-llm-wiki.git
cd plaud-to-obsidian-llm-wiki

# 수동 실행용 (선택)
echo 'export PLAUD_RAW_DIR="$HOME/Obsidian/raw"' >> ~/.zshrc

# 자동 실행 등록: 로그인 시 + 10분마다
PLAUD_RAW_DIR=~/Obsidian/raw ./install.sh
```

제거: `./install.sh uninstall`

## 환경변수
| 이름 | 필수 | 기본값 | 설명 |
|---|---|---|---|
| `PLAUD_RAW_DIR` | ✅ | — | 내보낼 폴더 |
| `PLAUD_STATE_DIR` | | `~/Library/Application Support/plaud-sync` | `state.json`, `sync.log`, `launchd.err` |
| `PLAUD_SYNC_INTERVAL` | | `600` | 자동 실행 주기(초), `install.sh` 전용 |

## 수동 실행
```sh
python3 plaud_sync.py                    # 최근 녹음 100건 확인
python3 plaud_sync.py --dry-run          # 내보낼 대상만 표시
python3 plaud_sync.py --since 2026-10-01 # 이 날짜 이후 녹음
python3 plaud_sync.py --all              # 전체 다시 훑기 (누락 복구)
python3 plaud_sync.py --id <ID> --force  # 특정 녹음을 새 파일로 다시 내보내기
```

로그인이 만료되면 macOS 알림이 뜹니다. MCP 클라이언트에서 다시 로그인하세요.

## 라이선스
MIT
