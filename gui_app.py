#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import subprocess, urllib.request
from aitrade.dashboard.live_server import run_control_center_server

def main() -> None:
    url = "http://127.0.0.1:8765"
    try:
        urllib.request.urlopen(url + "/api/state", timeout=0.8)
        subprocess.run(["/usr/bin/open", url], check=False)
        return
    except Exception:
        pass
    run_control_center_server(port=8765, open_browser=False)

if __name__ == "__main__":
    main()
