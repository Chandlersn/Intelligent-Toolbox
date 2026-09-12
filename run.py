#!/usr/bin/env python3
"""一键启动收藏箱。

架构封装后后端在 src/，前端静态在 web/。本脚本从项目根启动，
等价于直接 `python src/server.py`，仅作便捷入口。
"""
import os
import sys

if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    # 进本项目方案：若仓库自带 .venv 且当前不是用它启动，则改用 venv python 启动，
    # 这样抖音转录依赖的 faster-whisper 才 import 得到（缺失时 app 仍降级运行，不崩）。
    venv_py = os.path.join(root, ".venv", "Scripts", "python.exe")
    if os.path.isfile(venv_py) and os.path.abspath(sys.executable) != os.path.abspath(venv_py):
        os.execv(venv_py, [venv_py, os.path.abspath(__file__)])
    sys.path.insert(0, os.path.join(root, "src"))
    import server

    server.main() if hasattr(server, "main") else server.run()
