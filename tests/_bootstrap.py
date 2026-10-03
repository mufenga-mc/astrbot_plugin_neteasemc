# -*- coding: utf-8 -*-
"""测试用的引导：把「仓库根」和「AstrBot 的 core 目录」加进 sys.path。

为什么需要它：AstrBot 的 venv 里**并没有装 astrbot 这个包** ——
它是运行时把自己的 `core` 目录加进 sys.path 才能 import 的。
所以测试想 import `main.py`（里面 `from astrbot.api import ...`）就得先找到那个 core。

找的顺序：
    1. 环境变量 ASTRBOT_CORE
    2. 当前目录往上找（在 AstrBot 安装目录里跑的情况）
    3. Windows 启动器的默认位置 ~/.astrbot_launcher/instances/*/core
"""

import glob
import os
import sys


def is_core(path):
    return os.path.isfile(os.path.join(path, "astrbot", "api", "star", "__init__.py"))


def find_core():
    env = os.environ.get("ASTRBOT_CORE", "")
    if env and is_core(env):
        return env

    here = os.path.abspath(os.getcwd())
    for _ in range(4):
        if is_core(here):
            return here
        candidate = os.path.join(here, "core")
        if is_core(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent

    base = os.path.join(os.path.expanduser("~"), ".astrbot_launcher", "instances")
    for path in sorted(glob.glob(os.path.join(base, "*", "core"))):
        if is_core(path):
            return path
    return ""


def setup():
    """加好路径，返回 (仓库根, AstrBot core 目录)。core 找不到就是空串。

    注意顺序：**仓库根必须排在 sys.path 最前面** —— AstrBot 的 core 目录里也有一个
    `main.py`（它自己的入口），要是 core 排在前面，`import main` 就会拿到它，
    于是 `from main import McBridge` 报 ImportError。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    core = find_core()

    if core and core not in sys.path:
        sys.path.insert(0, core)
    if root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    return root, core
