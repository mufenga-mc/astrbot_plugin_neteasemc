#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""@ 解析逻辑的离线自测（不需要 AstrBot 启动，也不用连游戏）。

用 AstrBot 那个 venv 的 python 跑：
    <实例目录>/venv/Scripts/python.exe tests/test_mentions.py

做法：拿真的 `McBridge._build_mentions`，配上假的 `_lookup_member`，
所以测的是真代码里的解析逻辑，不是复制一份。
"""

import asyncio
import os
import sys

import _bootstrap

_bootstrap.setup()          # 仓库根 + AstrBot core 都加进 sys.path

from main import McBridge  # noqa: E402

MEMBERS = {"慕枫": 10001, "浅葱": 10002}


class Stub(object):
    """只提供 _build_mentions 需要的那一个依赖。"""

    async def _lookup_member(self, name):
        return MEMBERS.get(name)


Stub._build_mentions = McBridge._build_mentions


def describe(components):
    out = []
    for comp in components:
        cls = type(comp).__name__
        qq = getattr(comp, "qq", "")
        out.append("%s(%s)" % (cls, qq))
    return out


async def main():
    stub = Stub()
    failed = []

    # ① _strip_leading_mentions：AstrBot 会把「非机器人的 @」写成 @昵称(qq) 留在文本里
    strip_cases = [
        ("#你好", "#你好"),
        (" #你好 ", "#你好"),
        (" @浅葱(10002)  #你好 ", "#你好"),
        ("@浅葱(10002) #你好", "#你好"),
        ("@a(1) @b(2) #你好", "#你好"),
        ("@浅葱(10002) 你好", "你好"),
        ("你好", "你好"),
    ]
    for src, want in strip_cases:
        got = McBridge._strip_leading_mentions(src)
        ok = got == want
        print("%s strip %-26r -> %r" % ("OK " if ok else "BAD", src, got))
        if not ok:
            failed.append("strip:%s" % src)

    cases = [
        # (输入, 期望组件, 期望剩余文本, 期望没找到的名字)
        ("@123456 你好", ["At(123456)"], "你好", []),
        ("@慕枫 你好", ["At(10001)"], "你好", []),
        ("@全体成员 集合", ["AtAll(all)"], "集合", []),
        ("@不存在 你好", [], "你好", ["不存在"]),
        ("你好", [], "你好", []),
        ("@慕枫 @123456 你们都来", ["At(10001)", "At(123456)"], "你们都来", []),
        ("@浅葱", ["At(10002)"], "", []),
    ]

    for text, want_comps, want_rest, want_missing in cases:
        comps, rest, missing = await stub._build_mentions(text)
        got = describe(comps)
        ok = got == want_comps and rest == want_rest and missing == want_missing
        print("%s %-28r -> %s | 剩下=%r | 没找到=%s" % ("OK " if ok else "BAD", text, got, rest, missing))
        if not ok:
            failed.append(text)

    if failed:
        print("[mention-test] 失败：%s" % failed)
        return 1
    print("[mention-test] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
