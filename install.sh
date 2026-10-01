#!/bin/zsh
# launchd 자동 동기화 설치/제거.
#   PLAUD_RAW_DIR=~/Obsidian/raw ./install.sh   # 설치 (기본 10분마다 + 로그인 시)
#   ./install.sh uninstall                       # 제거
# 선택: PLAUD_SYNC_INTERVAL(초, 기본 600), PLAUD_STATE_DIR, PLAUD_NOTE_WAIT_HOURS(기본 24), PLAUD_MAX_RETRIES(기본 10)
LABEL=local.plaud-sync
PLIST=~/Library/LaunchAgents/$LABEL.plist
DIR=${0:A:h}

if [[ $1 == uninstall ]]; then
  launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null
  rm -f $PLIST; echo "제거됨"; exit 0
fi

# 입력은 모두 기존 에이전트를 내리기(bootout) 전에 검사한다. 설치가 실패해도 기존 자동 실행은 그대로 남는다.
fail() { echo "$1" >&2; exit 1; }
[[ -n $PLAUD_RAW_DIR ]] || fail "PLAUD_RAW_DIR를 지정하세요. 예: PLAUD_RAW_DIR=~/Obsidian/raw ./install.sh"
RAW=${PLAUD_RAW_DIR:A}
[[ -d $RAW ]] || fail "폴더 없음: $RAW"
STATE=${PLAUD_STATE_DIR:-$HOME/Library/Application Support/plaud-sync}
STATE=${STATE:A}   # launchd는 상대경로를 / 기준으로 해석하므로 절대경로로
INTERVAL=${PLAUD_SYNC_INTERVAL:-600}
[[ $INTERVAL =~ '^[1-9][0-9]*$' ]] || fail "PLAUD_SYNC_INTERVAL은 1 이상의 정수(초)여야 합니다: $INTERVAL"
# 잘못된 값이 plist에 들어가면 자동 실행이 매번 인자 오류로 끝나므로 미리 막는다
if [[ -n $PLAUD_NOTE_WAIT_HOURS ]]; then
  if [[ ! $PLAUD_NOTE_WAIT_HOURS =~ '^[0-9]+([.][0-9]+)?$' ]] || (( PLAUD_NOTE_WAIT_HOURS > 8760 )); then
    fail "PLAUD_NOTE_WAIT_HOURS는 0부터 8760 사이의 숫자(시간)여야 합니다: $PLAUD_NOTE_WAIT_HOURS"
  fi
fi
if [[ -n $PLAUD_MAX_RETRIES ]]; then
  if [[ ! $PLAUD_MAX_RETRIES =~ '^[0-9]+$' ]] || (( 10#$PLAUD_MAX_RETRIES < 1 || 10#$PLAUD_MAX_RETRIES > 1000 )); then
    fail "PLAUD_MAX_RETRIES는 1부터 1000 사이의 정수여야 합니다: $PLAUD_MAX_RETRIES"
  fi
fi
mkdir -p "$STATE" ~/Library/LaunchAgents || fail "폴더를 만들 수 없음: $STATE"

# plist(XML)에 넣을 값은 &, <, > 를 이스케이프
xml() { local v=${1//&/&amp;}; v=${v//</&lt;}; print -r -- ${v//>/&gt;}; }
X_DIR=$(xml "$DIR"); X_RAW=$(xml "$RAW"); X_STATE=$(xml "$STATE")
EXTRA_ENV=""
[[ -n $PLAUD_NOTE_WAIT_HOURS ]] && EXTRA_ENV+="    <key>PLAUD_NOTE_WAIT_HOURS</key><string>$(xml "$PLAUD_NOTE_WAIT_HOURS")</string>"$'\n'
[[ -n $PLAUD_MAX_RETRIES ]] && EXTRA_ENV+="    <key>PLAUD_MAX_RETRIES</key><string>$(xml "$PLAUD_MAX_RETRIES")</string>"$'\n'

# 새 plist를 임시 파일에 먼저 써 보고, 성공했을 때만 기존 에이전트를 내리고 바꿔 끼운다
TMP=$PLIST.tmp
cat > $TMP <<P || { rm -f $TMP; fail "plist를 쓸 수 없음: $TMP"; }
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$X_DIR/plaud_sync.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PLAUD_RAW_DIR</key><string>$X_RAW</string>
    <key>PLAUD_STATE_DIR</key><string>$X_STATE</string>
${EXTRA_ENV}    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$X_STATE/launchd.err</string>
</dict></plist>
P

# plist 교체가 실패해도 기존 에이전트가 살아 있도록 mv를 bootout보다 먼저 한다 (bootout은 라벨로 내리므로 파일과 무관)
mv -f $TMP $PLIST || { rm -f $TMP; fail "plist를 옮길 수 없음: $PLIST"; }
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null
# bootout 직후에는 bootstrap이 가끔 "Input/output error"로 실패하므로 몇 번 다시 시도한다
for n in 1 2 3; do
  if launchctl bootstrap gui/$(id -u) $PLIST; then
    echo "설치됨: ${INTERVAL}초마다 실행 → $RAW (로그: $STATE/sync.log)"; exit 0
  fi
  (( n < 3 )) && { echo "등록 실패, 1초 뒤 다시 시도 ($n/3)" >&2; sleep 1; }
done
echo "launchd 등록 실패: launchctl bootstrap gui/$(id -u) $PLIST 를 직접 실행해 보세요" >&2
exit 1
