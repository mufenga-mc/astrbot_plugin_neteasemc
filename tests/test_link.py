#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""GameLink 的离线自测（不需要游戏、不需要 AstrBot）。

用 AstrBot 那个 venv 的 python 跑：
    <实例目录>/venv/Scripts/python.exe tests/test_link.py
"""

import asyncio
import os
import sys

import _bootstrap

_bootstrap.setup()          # 仓库根（这个测试不需要 astrbot）

from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError  # noqa: E402
from mc_ws_server import GameLink  # noqa: E402


async def case_basic():
    got = []

    async def on_text(text):
        got.append(text)
        await link.send("回显: " + text)

    link = GameLink(port=18801, on_text=on_text, log=lambda m: print("   [link]", m))
    await link.start()
    assert not link.connected, "还没人连，connected 应为 False"

    async with ClientSession() as session:
        async with session.ws_connect("ws://127.0.0.1:18801/") as ws:
            await asyncio.sleep(0.1)
            assert link.connected, "接入后 connected 应为 True"

            await ws.send_str("hello from game")
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert msg.data == "回显: hello from game", msg.data
            assert got == ["hello from game"], got

            assert await link.send("主动推送") is True
            msg2 = await asyncio.wait_for(ws.receive(), timeout=5)
            assert msg2.data == "主动推送", msg2.data

            # 第二个连接应该把第一个踢掉
            # 注意：客户端要「读一下」才会知道自己被关了（不读的话 closed 一直是 False）
            async with session.ws_connect("ws://127.0.0.1:18801/") as ws2:
                msg_kick = await asyncio.wait_for(ws.receive(), timeout=3)
                assert msg_kick.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED), msg_kick.type
                await asyncio.sleep(0.1)
                assert ws.closed, "被踢掉的那条应该是 closed"

    await asyncio.sleep(0.3)
    assert not link.connected, "断开后 connected 应为 False"
    assert await link.send("没人连时") is False, "没人连时 send 应返回 False"
    await link.stop()
    print("[test] 基本收发 / 踢旧连接 / 断开状态：通过")


async def case_token():
    link = GameLink(port=18802, token="secret", log=lambda m: print("   [link]", m))
    await link.start()
    async with ClientSession() as session:
        rejected = False
        try:
            async with session.ws_connect("ws://127.0.0.1:18802/") as ws:
                await ws.receive()
        except WSServerHandshakeError as err:
            rejected = err.status == 403
        assert rejected, "不带 token 应该被 403 拒掉"

        async with session.ws_connect("ws://127.0.0.1:18802/?token=secret") as ws:
            await asyncio.sleep(0.1)
            assert link.connected, "带对 token 应该能连上"
            await ws.send_str("ping")
    await link.stop()
    print("[test] token 校验（403 / 放行）：通过")


async def main():
    await case_basic()
    await case_token()
    print("[test] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
