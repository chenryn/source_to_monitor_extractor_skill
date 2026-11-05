#!/usr/bin/env python3
"""
[已弃用] 保留此文件仅为兼容旧命令。
请改用 diff_signals.py —— 它同时支持日志 / 指标 / 链路三类信号的增量对比。

    python diff_signals.py old.json new.json
"""

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    print("[提示] diff_loggers.py 已由 diff_signals.py 取代（支持日志/指标/链路），"
          "本次将自动转发。\n", file=sys.stderr)
    target = Path(__file__).parent / "diff_signals.py"
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")
