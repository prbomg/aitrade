#!/bin/bash
export TK_SILENCE_DEPRECATION=1
cd "/Users/sergejrubcov/AItrade"
/usr/bin/python3 "/Users/sergejrubcov/AItrade/gui_app.py" --demo-trade
osascript -e 'tell application "Terminal" to close (every window whose name contains "AItrade")' >/dev/null 2>&1 &
exit 0
