#!/bin/zsh
# launchd 자동 동기화 설치/제거.
#   PLAUD_RAW_DIR=~/Obsidian/raw ./install.sh   # 설치 (기본 10분마다 + 로그인 시)
#   ./install.sh uninstall                       # 제거
# 선택: PLAUD_SYNC_INTERVAL(초, 기본 600), PLAUD_STATE_DIR
LABEL=local.plaud-sync
PLIST=~/Library/LaunchAgents/$LABEL.plist
DIR=${0:A:h}

launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null
if [[ $1 == uninstall ]]; then rm -f $PLIST; echo "제거됨"; exit 0; fi

if [[ -z $PLAUD_RAW_DIR ]]; then
  echo "PLAUD_RAW_DIR를 지정하세요. 예: PLAUD_RAW_DIR=~/Obsidian/raw ./install.sh" >&2; exit 1
fi
RAW=${PLAUD_RAW_DIR:A}
[[ -d $RAW ]] || { echo "폴더 없음: $RAW" >&2; exit 1; }
STATE=${PLAUD_STATE_DIR:-$HOME/Library/Application Support/plaud-sync}
INTERVAL=${PLAUD_SYNC_INTERVAL:-600}
mkdir -p "$STATE" ~/Library/LaunchAgents

cat > $PLIST <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>$DIR/plaud_sync.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PLAUD_RAW_DIR</key><string>$RAW</string>
    <key>PLAUD_STATE_DIR</key><string>$STATE</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>$STATE/launchd.err</string>
</dict></plist>
P
launchctl bootstrap gui/$(id -u) $PLIST && echo "설치됨: ${INTERVAL}초마다 실행 → $RAW (로그: $STATE/sync.log)"
