# -*- coding: utf-8 -*-
"""校验受支持 Python 环境中的依赖版本与锁定清单一致。"""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys


def main() -> int:
    if sys.version_info[:2] not in {(3, 11), (3, 12), (3, 13)}:
        print("项目仅支持 Python 3.11～3.13。")
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
