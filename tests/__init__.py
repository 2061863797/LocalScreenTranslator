"""本地屏译（LocalScreen Translator）回归测试。

在导入任何 app 模块前把数据目录指向临时文件夹，
避免测试读写真实的 config.json、data.db 与 app.log。
"""

import atexit
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="localscreen-tests-")
os.environ["LOCALSCREEN_DATA_DIR"] = _DATA_DIR
atexit.register(shutil.rmtree, _DATA_DIR, ignore_errors=True)
