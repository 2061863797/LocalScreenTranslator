# -*- coding: utf-8 -*-
"""校验发布环境与 Python 3.12 锁定清单完全一致。"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        print("发布锁定清单针对 Python 3.12；当前 Python 版本不匹配。")
        return 1

    lock_path = Path(__file__).resolve().parents[1] / "requirements-lock.txt"
    mismatches = []
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise ValueError(f"锁定清单存在非固定版本：{line}")
        name, expected = line.split("==", 1)
        try:
            actual = version(name)
        except PackageNotFoundError:
            actual = "<missing>"
        if actual != expected:
            mismatches.append(f"{name}: expected {expected}, installed {actual}")

    if mismatches:
        print("发布依赖版本不匹配：")
        for mismatch in mismatches:
            print(f"  {mismatch}")
        return 1
    print("发布依赖与 requirements-lock.txt 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
