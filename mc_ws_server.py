# -*- coding: utf-8 -*-
"""给「我的世界中国版」那侧用的 WebSocket 服务端（纯 aiohttp，不依赖 AstrBot）。

特意单独放一个文件：这样它能脱离 AstrBot 直接跑测试。

约定（跟游戏侧一致）：
    - 游戏连上来之后，用文本帧收发；
    - 收到文本 → 交给 on_text 回调；
    - send() 把文本推给游戏（没连接时返回 False）。
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from aiohttp import WSMsgType, web


class GameLink:
    """只服务一个游戏连接的 WebSocket 服务端。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8800,
        token: str = "",
        on_text: Callable[[str], Awaitable[None]] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.host = host
        self.port = port
        self.token = token or ""
        self._on_text = on_text
        self._log = log or (lambda _m: None)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._ws: web.WebSocketResponse | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 状态
    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._handle)
        app.router.add_get("/ws", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        self._log("WebSocket 服务端已启动：ws://%s:%d/" % (self.host, self.port))
        if self.token:
            self._log("已设置 token，游戏侧地址要带 ?token=...")

    async def stop(self) -> None:
        async with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        self._log("WebSocket 服务端已停止")

    # ------------------------------------------------------------------ 推给游戏
    async def send(self, text: str) -> bool:
        ws = self._ws
        if ws is None or ws.closed:
            return False
        try:
            await ws.send_str(text)
            return True
        except Exception as err:
            self._log("推给游戏失败：%r" % (err,))
            return False

    # ------------------------------------------------------------------ 连接处理
    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if self.token:
            given = request.query.get("token", "")
            if given != self.token:
                self._log("拒绝一个 token 不对的连接：%s" % (request.remote,))
                return web.Response(status=403, text="bad token")

        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
        await ws.prepare(request)

        async with self._lock:
            old, self._ws = self._ws, ws
        if old is not None and not old.closed:
            self._log("已经有连接了，踢掉旧的")
            try:
                await old.close()
            except Exception:
                pass

        self._log("游戏已接入：%s" % (request.remote,))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if self._on_text is not None:
                        try:
                            await self._on_text(msg.data)
                        except Exception as err:
                            self._log("处理游戏消息出错：%r" % (err,))
                elif msg.type == WSMsgType.ERROR:
                    self._log("连接出错：%r" % (ws.exception(),))
        finally:
            async with self._lock:
                if self._ws is ws:
                    self._ws = None
            self._log("游戏断开：%s" % (request.remote,))
        return ws
