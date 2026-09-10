# -*- coding: utf-8 -*-
"""启动入口：python run.py

进入主程序并使用项目内便携 runtime。
"""

import sys

if "--smoke-check" in sys.argv:
    from app.main import main
    from PySide6.QtCore import qVersion
    print(f"Smoke check OK: Qt {qVersion()}")
    sys.exit(0)

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
