#!/usr/bin/env python3
"""一键启动收藏箱。

架构封装后后端在 src/，前端静态在 web/。本脚本从项目根启动，
等价于直接 `python src/server.py`，仅作便捷入口。
"""
import os
import sys

if __name__ == "__main__":
    root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(root, "src"))
    import server

    server.main() if hasattr(server, "main") else server.run()
