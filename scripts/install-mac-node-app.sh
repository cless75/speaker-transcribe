#!/bin/bash
# install-mac-node-app.sh — собрать .app-обёртку узла и переустановить LaunchAgent.
#
# ЗАЧЕМ .app. Google Drive File Provider не материализует dataless-файлы по запросу
# «голого» launchd-процесса: open()/read() возвращает EDEADLK (errno 11), а
# NSFileCoordinator виснет в _withAccessArbiter навсегда. Проверено на M4-MAC:
# из ssh-сессии тот же файл читается, из LaunchAgent — нет, независимо от
# ProcessType, интерпретатора, SessionCreate и наличия Full Disk Access у бинарника.
# Тот же код, запущенный через LaunchServices (`open -a <bundle>`), материализует
# файлы штатно (проверено полным чтением 91 МБ). Поэтому LaunchAgent не зовёт
# вотчер напрямую, а просит LaunchServices запустить .app-обёртку.
#
# Идемпотентно: пересоздаёт бандл и перезагружает агента.
set -euo pipefail

APP_DIR="$HOME/Applications/SpeakerTranscribeNode.app"
REPO="$HOME/work/speaker-transcribe"
LABEL="com.clessd.speaker-transcribe"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/speaker-transcribe"

mkdir -p "$APP_DIR/Contents/MacOS" "$LOG_DIR"

cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIdentifier</key><string>com.clessd.speaker-transcribe-node</string>
    <key>CFBundleName</key><string>SpeakerTranscribeNode</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSBackgroundOnly</key><true/>
</dict>
</plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
# Запускается LaunchServices (`open -n -a`) из LaunchAgent com.clessd.speaker-transcribe.
# LaunchServices не отдаёт stdout/stderr в launchd, поэтому лог открываем сами.
# Ротация — здесь, ДО открытия дескриптора: иначе mv увёл бы уже открытый файл.
set -u
LOG_DIR="$HOME/Library/Logs/speaker-transcribe"
LOG_MAX_BYTES=$((10 * 1024 * 1024))
LOG_KEEP=5
mkdir -p "$LOG_DIR"
for fname in stdout.log stderr.log; do
  fpath="$LOG_DIR/$fname"
  [ -f "$fpath" ] || continue
  size=$(stat -f%z "$fpath" 2>/dev/null || echo 0)
  [ "$size" -gt "$LOG_MAX_BYTES" ] || continue
  [ -f "$fpath.$LOG_KEEP" ] && rm -f "$fpath.$LOG_KEEP"
  i=$((LOG_KEEP - 1))
  while [ "$i" -ge 1 ]; do
    [ -f "$fpath.$i" ] && mv "$fpath.$i" "$fpath.$((i + 1))"
    i=$((i - 1))
  done
  mv "$fpath" "$fpath.1"
done
exec >> "$LOG_DIR/stderr.log" 2>&1
exec "$HOME/work/speaker-transcribe/scripts/watch-mac.sh"
LAUNCHER
chmod +x "$APP_DIR/Contents/MacOS/launcher"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>-n</string>
        <string>-a</string>
        <string>$APP_DIR</string>
    </array>
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/launchd-open.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/launchd-open.log</string>
</dict>
</plist>
PLIST

plutil -lint "$PLIST"

UID_NUM="$(id -u)"
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"

echo "installed: $APP_DIR"
echo "agent: $LABEL"
