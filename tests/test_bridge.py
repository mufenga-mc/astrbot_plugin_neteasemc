#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""游戏 → QQ 这条路的自测（重点是「失败时游戏里看得见」）。

以前这几种失败只写 AstrBot 控制台，玩游戏的人完全看不到，
症状就是「游戏里发了消息，QQ 那边没反应」。现在应该回传一句 `[桥] 原因`。

跑法：
    <实例目录>/venv/Scripts/python.exe tests/test_bridge.py
"""

import asyncio
import os
import sys

import _bootstrap

_bootstrap.setup()

from main import McBridge  # noqa: E402


class StubLink(object):
    connected = True

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return True


class StubContext(object):
    def __init__(self, result=True, boom=None, platform=None):
        self.result = result
        self.boom = boom
        self.sent = []
        self.platform = platform

    async def send_message(self, session, chain):
        if self.boom:
            raise RuntimeError(self.boom)
        self.sent.append((session, chain))
        return self.result

    def get_platform_inst(self, platform_id):
        return self.platform


class StubClient(object):
    """假的 CQHttp 客户端：只提供群成员列表。"""

    def __init__(self, members=None, boom=None):
        self.members = members or []
        self.boom = boom
        self.calls = []

    async def get_group_member_list(self, group_id=None):
        self.calls.append(group_id)
        if self.boom:
            raise RuntimeError(self.boom)
        return self.members


class StubPlatform(object):
    def __init__(self, client):
        self._client = client

    def get_client(self):
        return self._client


class Stub(object):
    _on_game_text = McBridge._on_game_text
    _notify_game = McBridge._notify_game
    _session = lambda self: self._session_value      # noqa: E731
    _ensure_group_context = McBridge._ensure_group_context
    _lookup_member = McBridge._lookup_member
    _members = McBridge._members
    _build_mentions = McBridge._build_mentions
    _split_speaker = staticmethod(McBridge._split_speaker)

    def __init__(self, link, ctx, config, session=""):
        self.link = link
        self.context = ctx
        self.config = config
        self._session_value = session
        self.ai_calls = []
        self._bot = None
        self._group_id = ""
        self._member_cache = {}

    async def _handle_ai_chat(self, prompt):
        self.ai_calls.append(prompt)


SESSION = "default:GroupMessage:1072401481"


def make(result=True, boom=None, session=SESSION, config=None, platform=None):
    cfg = {"ai_prefix": "*", "forward_game_to_qq": True, "at_resolve": True}
    cfg.update(config or {})
    link = StubLink()
    ctx = StubContext(result=result, boom=boom, platform=platform)
    return Stub(link, ctx, cfg, session), link, ctx


async def main():
    failed = []

    def check(name, ok, detail=""):
        print("%s %-30s %s" % ("OK " if ok else "BAD", name, detail))
        if not ok:
            failed.append(name)

    # ① 正常：转发给 QQ，游戏里不吭声
    stub, link, ctx = make()
    await stub._on_game_text("慕枫: 你好")
    ok = len(ctx.sent) == 1 and link.sent == []
    check("正常转发", ok, "QQ %d 条 / 回传 %r" % (len(ctx.sent), link.sent))
    check("发给了绑定会话", ctx.sent and ctx.sent[0][0] == SESSION)

    # ② 没绑定会话 → 游戏里要看见原因（以前是静默丢弃）
    stub, link, ctx = make(session="")
    await stub._on_game_text("你好")
    ok = ctx.sent == [] and link.sent == ["[桥] 还没绑定 QQ 会话 —— 在 QQ 群里发一次 /mc bind"]
    check("没绑定时游戏可见", ok, repr(link.sent))

    # ③ send_message 返回 False（掉线）→ 以前完全静默，现在要提示
    stub, link, ctx = make(result=False)
    await stub._on_game_text("你好")
    got = link.sent[0] if link.sent else ""
    ok = len(link.sent) == 1 and got.startswith("[桥] 转发到 QQ 失败")
    check("返回 False 时提示", ok, repr(link.sent))

    # ④ send_message 抛异常 → 也要提示
    stub, link, ctx = make(boom="timeout")
    await stub._on_game_text("你好")
    got = link.sent[0] if link.sent else ""
    ok = len(link.sent) == 1 and got.startswith("[桥] 转发到 QQ 出错")
    check("抛异常时提示", ok, repr(link.sent))

    # ⑤ AI 那路不受影响：不进 QQ，走 AI
    stub, link, ctx = make()
    await stub._on_game_text("*你好")
    ok = ctx.sent == [] and stub.ai_calls == ["你好"]
    check("AI 那路照旧", ok, "ai=%r" % (stub.ai_calls,))

    # ⑥ 关掉游戏→QQ 开关时，什么都不发
    stub, link, ctx = make(config={"forward_game_to_qq": False})
    await stub._on_game_text("你好")
    ok = ctx.sent == [] and link.sent == [] and stub.ai_calls == []
    check("关掉转发就安静", ok, "QQ %d / 回传 %r" % (len(ctx.sent), link.sent))

    # ⑦ 重装之后群里还没人说话：也要能从绑定会话里拿到群号 + 机器人
    client = StubClient(members=[{"nickname": "慕枫", "card": "", "user_id": 10001}])
    stub, link, ctx = make(platform=StubPlatform(client))
    ok = stub._ensure_group_context() is True
    check("从会话解析群上下文", ok, "群=%r" % (stub._group_id,))
    await stub._on_game_text("@慕枫 你好")
    chain = ctx.sent[0][1].chain if ctx.sent else []
    ats = [getattr(c, "qq", None) for c in chain if type(c).__name__.startswith("At")]
    ok = len(ctx.sent) == 1 and ats == [10001] and client.calls == [1072401481]
    check("能 @ 到人（昵称解析）", ok, "qq=%r 查的群=%r" % (ats, client.calls))

    # ⑧ 拿不到平台时：退化成纯文本，并且在群里说一句
    stub, link, ctx = make(platform=None)
    await stub._on_game_text("@慕枫 你好")
    first = ctx.sent[0][1].chain if ctx.sent else []
    plains = [c.text for c in first if type(c).__name__ == "Plain"]
    notice = ""
    if len(ctx.sent) > 1:
        notice = "".join(c.text for c in ctx.sent[1][1].chain if type(c).__name__ == "Plain")
    ok = plains == ["你好"] and u"没找到群成员" in notice
    check("拿不到平台时退化", ok, "正文=%r 提示=%r" % (plains, notice))

    # ⑨ 说话人前缀不能挡住 @（模组会发来「名字: @某人 你好」）
    client = StubClient(members=[{"nickname": "慕枫", "card": "", "user_id": 10001}])
    stub, link, ctx = make(platform=StubPlatform(client))
    await stub._on_game_text(u"慕枫MuFeng: @3885925685 你好")
    chain = ctx.sent[0][1].chain if ctx.sent else []
    kinds = [type(c).__name__ for c in chain]
    ats = [getattr(c, "qq", None) for c in chain if type(c).__name__.startswith("At")]
    plains = [getattr(c, "text", None) for c in chain if type(c).__name__ == "Plain"]
    ok = ats == [3885925685] and plains[:1] == [u"慕枫MuFeng: "] and u"你好" in "".join(plains)
    check("说话人不挡住 @QQ号", ok, "chain=%r" % (kinds,))

    del ctx.sent[:]
    stub, link, ctx = make(platform=StubPlatform(client))
    await stub._on_game_text(u"慕枫MuFeng: @慕枫 你好")
    ats = [getattr(c, "qq", None) for c in ctx.sent[0][1].chain
           if type(c).__name__.startswith("At")] if ctx.sent else []
    check("说话人不挡住 @昵称", ats == [10001], "qq=%r" % (ats,))

    del ctx.sent[:]
    stub, link, ctx = make(platform=StubPlatform(client))
    await stub._on_game_text(u"慕枫MuFeng: 你好")     # 没有 @：一个字都不能被切
    plains = [getattr(c, "text", None) for c in ctx.sent[0][1].chain] if ctx.sent else []
    check("没 @ 时原样转发", plains == [u"慕枫MuFeng: 你好"], repr(plains))

    if failed:
        print("[bridge-test] 失败：%s" % failed)
        return 1
    print("[bridge-test] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
