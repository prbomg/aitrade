#!/bin/bash
# Двойной клик по этому файлу в Finder запускает графический Центр Управления AItrade без работы в терминале
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"
nohup /usr/bin/python3 "$DIR/gui_app.py" >/dev/null 2>&1 &
osascript -e 'tell application "Terminal" to close (every window whose name contains "AItrade")' >/dev/null 2>&1 &
exit 0
