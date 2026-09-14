#!/usr/bin/env python3
"""从 Plugin 或项目安装位置启动同一份 DloopUI 运行时。"""

from __future__ import annotations

from pathlib import Path
import runpy
import sys


sys.dont_write_bytecode = True


def runtime_path() -> Path:
    current = Path(__file__).resolve()
    for ancestor in current.parents:
        for candidate in (
            ancestor / "runtime" / "dloop_ui.py",
            ancestor / "Tools" / "FeatureArchive" / "dloop_ui.py",
        ):
            if candidate.is_file() and candidate.resolve() != current:
                return candidate
    raise SystemExit("当前 DLoop 发行物缺少 DloopUI 运行时。")


if __name__ == "__main__":
    runpy.run_path(str(runtime_path()), run_name="__main__")
