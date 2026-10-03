# -*- coding: utf-8 -*-
"""AstrBot 插件：把《我的世界》中国版和 QQ 接起来（双向）。

    游戏里  #你好              →  转发到绑定的 QQ 会话
    游戏里  #@某人 你好         →  QQ 里真的 @ 到他（见下面「@ 的规则」）
    QQ 里   #你好              →  推给游戏，聊天栏显示 [WS] [QQ] 昵称: 你好
    QQ 里   @机器人 你好        →  同上（被 @ 时不用打 #）

@ 的规则（游戏 → QQ）：
    @123456789        按 QQ 号 @
    @昵称 / @群名片     按群成员昵称或群名片 @（会去查群成员列表，缓存 60 秒）
    @全体成员           @全体成员（aiocqhttp 的 at all）
    认不出来时：那条 @ 会退化成纯文本，并在群里提示一句。

游戏那边一行都不用改 —— 它只管把 `#文本` 原样发过来。
（配合 game 侧的目标文件：ws://127.0.0.1:8800/，游戏里 #连接）

实测环境：AstrBot 4.28.2 / Python 3.12 / aiohttp 3.14.3
"""

from __future__ import annotations

import json
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, AtAll, Plain
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .mc_ws_server import GameLink
except ImportError:  # 极端情况下插件不是以包的方式加载
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from mc_ws_server import GameLink

PLUGIN_NAME = "astrbot_plugin_neteasemc"
MEMBER_CACHE_TTL = 60.0     # 群成员列表缓存多久（秒）
AT_ALL_NAMES = ("全体成员", "全体", "all", "所有人")


@register(
    PLUGIN_NAME,
    "慕枫",
    "游戏 ↔ QQ 双向桥：《我的世界》里的消息转进 QQ（支持 @人），QQ 的消息推给游戏",
    "1.3.0",
)
class McBridge(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.link: GameLink | None = None
        self.target_session: str = ""
        self._state_path: str = ""
        self._bot = None                     # 最近一次 QQ 消息的 bot（拿群成员列表要用）
        self._group_id: str = ""
        self._member_cache: dict[str, tuple[float, list]] = {}
        self._ai_history: list[dict] = []     # 游戏 AI 的对话记忆（自己记，见 _remember）
        self._history_path: str = ""
        self._load_state()

    # ------------------------------------------------------------ 生命周期
    async def initialize(self):
        try:
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            if data_dir is not None:
                self._state_path = os.path.join(str(data_dir), "state.json")
                self._history_path = os.path.join(str(data_dir), "ai_history.json")
                self._load_state()
                self._load_ai_history()
        except Exception as err:
            logger.warning("[mc-bridge] 拿数据目录失败：%r", err)

        host = str(self.config.get("host", "127.0.0.1") or "127.0.0.1")
        port = int(self.config.get("port", 8800) or 8800)
        token = str(self.config.get("token", "") or "")
        self.link = GameLink(
            host=host,
            port=port,
            token=token,
            on_text=self._on_game_text,
            log=lambda m: logger.info("[mc-bridge] %s", m),
        )
        try:
            await self.link.start()
        except Exception as err:
            logger.error(
                "[mc-bridge] 起 WebSocket 服务端失败：%r（端口 %s 是不是被别的程序占了？）",
                err,
                port,
            )
            self.link = None

    async def terminate(self):
        if self.link is not None:
            await self.link.stop()
            self.link = None

    # ------------------------------------------------------------ 游戏 → QQ / AI
    async def _on_game_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return

        # *文本 → 和 AI 对话（不需要绑定 QQ 会话，走的是游戏 ⇄ AI）
        ai_prefix = str(self.config.get("ai_prefix", "*") or "")
        if ai_prefix and text.startswith(ai_prefix):
            await self._handle_ai_chat(text[len(ai_prefix):].strip())
            return

        if not self.config.get("forward_game_to_qq", True):
            return
        session = self._session()
        if not session:
            logger.warning("[mc-bridge] 游戏发来消息，但还没绑定 QQ 会话（在群里发 /mc bind）：%s", text)
            await self._notify_game("还没绑定 QQ 会话 —— 在 QQ 群里发一次 /mc bind")
            return

        chain = []
        missing = []
        if self.config.get("at_resolve", True):
            # 模组会给消息加说话人（`名字: 正文`），而 @ 可能跟在它后面 ——
            # 那就先把这层剥下来，解析完 @ 再把说话人拼回链条最前面。
            speaker, body = self._split_speaker(text)
            components, body, missing = await self._build_mentions(body)
            if speaker:
                chain.append(Plain(text=speaker))
            chain.extend(components)
            text = body
        if text:
            chain.append(Plain(text=text))
        if not chain:
            return
        try:
            ok = await self.context.send_message(session, MessageChain(chain=chain))
        except Exception as err:
            logger.error("[mc-bridge] 转发到 %s 失败：%r", session, err)
            await self._notify_game("转发到 QQ 出错：%s" % err)
            return
        if not ok:
            # send_message 失败时是返回 False，不抛异常 —— 不看返回值就会静默丢消息
            logger.error("[mc-bridge] send_message 返回 False（会话 %s）", session)
            await self._notify_game("转发到 QQ 失败：机器人可能掉线了，群里发 /mc status 看看")
        if missing:
            try:
                await self.context.send_message(
                    session,
                    MessageChain(chain=[Plain(text="（没找到群成员：%s）" % "、".join(missing))]),
                )
            except Exception:
                pass

    async def _notify_game(self, text: str):
        """把插件这边的故障回传到游戏聊天栏。

        为什么要这样：这些失败以前只写 AstrBot 控制台 —— 而玩游戏的人看不到控制台，
        表现就是「游戏里发了消息，QQ 那边一点反应都没有」，还查不出原因。
        现在游戏里会直接出现 `[桥] 原因`。
        """
        if self.link is None or not self.link.connected:
            return
        try:
            await self.link.send("[桥] " + text)
        except Exception as err:
            logger.debug("[mc-bridge] 回传游戏失败：%r", err)

    @staticmethod
    def _split_speaker(text: str):
        """剥掉模组加的说话人前缀（`名字: `），返回 (前缀含冒号空格, 正文)。

        **只在这层前缀挡住了开头的 @ 时才剥** —— 模组会把消息发成
        `慕枫MuFeng: @3885925685 你好`，而我们只认开头的 @；
        别的情况原样返回，免得把正文里正常的「xx: yy」切开。
        """
        head, sep, tail = text.partition(": ")
        if sep and tail.startswith("@"):
            return head + ": ", tail
        return "", text

    async def _build_mentions(self, text: str):
        """把文本开头的 @xxx 解析成 At 组件。

        返回 (组件列表, 剩下的文本, 没找到的名字列表)。
        """
        components = []
        missing = []
        rest = text.strip()
        while rest.startswith("@"):
            token, _, tail = rest.partition(" ")
            name = token[1:].strip()
            if not name:
                break
            if name in AT_ALL_NAMES:
                components.append(AtAll())
            elif name.isdigit():
                components.append(At(qq=int(name), name=name))
            else:
                qq = await self._lookup_member(name)
                if qq is None:
                    missing.append(name)
                else:
                    components.append(At(qq=qq, name=name))
            rest = tail.strip()
        return components, rest, missing

    def _ensure_group_context(self):
        """没收到 QQ 消息时，也从绑定会话里把「群号 + 机器人句柄」找出来。

        为什么需要：按昵称 @ 要查群成员列表，而这份能力以前只能靠「群里来过消息」
        顺便记下 `event.bot` 和 `event.group_id` —— 重装/重启之后如果群里还没人说话，
        这两个就是空的，`@某人` 只能退化成纯文本（实测踩到过）。

        绑定会话形如 `default:GroupMessage:1072401481`：第一段是平台 id，
        第三段是群号；平台对象能给出 aiocqhttp 的 CQHttp 客户端。
        """
        session = self._session()
        if not session:
            return False
        parts = session.split(":")
        if len(parts) >= 3 and parts[1] == "GroupMessage" and not self._group_id:
            self._group_id = parts[2]
        if self._bot is None and parts[0]:
            platform = None
            try:
                platform = self.context.get_platform_inst(parts[0])
            except Exception as err:
                logger.debug("[mc-bridge] get_platform_inst(%r) 失败：%r", parts[0], err)
            if platform is not None:
                getter = getattr(platform, "get_client", None)
                if callable(getter):
                    try:
                        self._bot = getter()
                    except Exception as err:
                        logger.warning("[mc-bridge] 取机器人句柄失败：%r", err)
        if self._bot is not None and self._group_id:
            return True
        logger.warning(
            "[mc-bridge] @某人 解析不了：%s（会话=%r）",
            "拿不到机器人句柄" if self._bot is None else "没解析出群号",
            session,
        )
        return False

    async def _lookup_member(self, name: str):
        """按昵称/群名片找群成员的 QQ 号。找不到返回 None。"""
        if not self._ensure_group_context():
            return None
        for member in await self._members():
            if str(member.get("nickname", "")) == name or str(member.get("card", "")) == name:
                return member.get("user_id")
        return None

    async def _members(self):
        """拿群成员列表（带 60 秒缓存）。"""
        group_id = self._group_id
        if not group_id:
            return []
        cached = self._member_cache.get(group_id)
        now = time.time()
        if cached and now - cached[0] < MEMBER_CACHE_TTL:
            return cached[1]
        data = []
        try:
            data = await self._bot.get_group_member_list(group_id=int(group_id))
        except Exception as err:
            logger.warning("[mc-bridge] 取群成员列表失败：%r", err)
        data = data or []
        self._member_cache[group_id] = (now, data)
        return data

    async def _handle_ai_chat(self, prompt: str):
        """游戏里 `*文本` → 调 AstrBot 当前使用的对话模型 → 把回复推回游戏。

        用的就是 AstrBot 里配的那个模型（含它的人设/系统提示），
        所以不用另配 key。会话名固定，因此游戏里能连续对话（有上下文记忆）。
        """
        label = str(self.config.get("ai_label", "[AI] ") or "")
        if self.link is None or not self.link.connected:
            return
        if not prompt:
            await self.link.send(label + "你想问什么？")
            return
        if self.config.get("ai_thinking_notice", False):
            await self.link.send(label + "（在想…）")

        provider = None
        try:
            provider = self.context.get_using_provider(umo=self._session() or None)
        except Exception as err:
            logger.warning("[mc-bridge] 取对话模型失败：%r", err)
        if provider is None:
            await self.link.send(label + "没有可用的对话模型（去 AstrBot 里配一个）")
            return

        # 注意：不要传 session_id —— 源码里写明了「会话 ID(此属性已经被废弃)」，
        # 它既不读历史也不存历史，传了会让人误以为有记忆。
        system_prompt = await self._resolve_system_prompt()
        contexts = self._ai_contexts()
        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=contexts or None,
                system_prompt=system_prompt,
            )
            reply = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception as err:
            logger.error("[mc-bridge] 调模型失败：%r", err)
            await self.link.send(label + "调用出错了：%s" % err)
            return

        if not reply:
            reply = "（模型没返回内容）"
        self._remember(prompt, reply)
        # 多行回复拆成多条推（游戏那边一行一条显示，太长还会自动折行）
        for line in reply.splitlines():
            line = line.strip()
            if line:
                await self.link.send(label + line)

    # ------------------------------------------------------------ AI 的记忆
    def _history_file(self) -> str:
        if self._history_path:
            return self._history_path
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_history.json")

    def _history_rounds(self) -> int:
        try:
            return int(self.config.get("ai_history_rounds", 6) or 0)
        except Exception:
            return 6

    def _ai_contexts(self):
        """取出要喂给模型的上下文（最近 N 轮）。0 轮 = 不带记忆。"""
        rounds = self._history_rounds()
        if rounds <= 0 or not self._ai_history:
            return []
        return list(self._ai_history[-rounds * 2:])

    def _remember(self, prompt: str, reply: str):
        """记一轮问答。

        为什么要自己记：`text_chat(session_id=...)` 里的 session_id 是废弃参数，
        根本没人去取历史 —— 不自记的话就是「每次都是第一次见面」。
        """
        rounds = self._history_rounds()
        if rounds <= 0:
            return
        self._ai_history.append({"role": "user", "content": prompt})
        self._ai_history.append({"role": "assistant", "content": reply})
        if len(self._ai_history) > rounds * 2:
            self._ai_history = self._ai_history[-rounds * 2:]
        self._save_ai_history()

    def _load_ai_history(self):
        try:
            path = self._history_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                if isinstance(data, list):
                    self._ai_history = [
                        item for item in data
                        if isinstance(item, dict) and item.get("role") and item.get("content")
                    ]
                logger.info("[mc-bridge] 游戏 AI 记忆：%d 条", len(self._ai_history))
        except Exception as err:
            logger.warning("[mc-bridge] 读 AI 记忆失败：%r", err)

    def _save_ai_history(self):
        try:
            path = self._history_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(self._ai_history, fp, ensure_ascii=False, indent=1)
        except Exception as err:
            logger.warning("[mc-bridge] 写 AI 记忆失败：%r", err)

    def _forget(self) -> int:
        count = len(self._ai_history) // 2
        self._ai_history = []
        self._save_ai_history()
        return count

    async def _resolve_system_prompt(self):
        """决定游戏这路 AI 用什么人设。

        优先级：
            1. ai_system_prompt  —— 手写的，最直接，填了就它说话
            2. ai_persona        —— 指定 AstrBot 里某个人格（按 persona_id）
            3. AstrBot 默认人格   —— ai_use_default_persona 开着时用（和 QQ 那边一致）

        **为什么要手动带上**：直接调 provider.text_chat 是绕过 AstrBot 对话管线的，
        所以 WebUI 里配的人格不会自动生效 —— 得我们自己取出来塞进去。
        """
        manual = str(self.config.get("ai_system_prompt", "") or "").strip()
        if manual:
            return manual

        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return None

        wanted = str(self.config.get("ai_persona", "") or "").strip()
        try:
            if wanted:
                persona = manager.get_persona_v3_by_id(wanted)
                if persona:
                    return str(persona.get("prompt") or "") or None
                # 按名字没取到，就把所有人格列出来对一遍
                for item in await manager.get_all_personas():
                    if str(getattr(item, "persona_id", "") or "") == wanted:
                        return str(getattr(item, "system_prompt", "") or "") or None
                logger.warning("[mc-bridge] 没找到人格 %r，改用默认人格（/mc persona 可以列出来）", wanted)

            if not self.config.get("ai_use_default_persona", True):
                return None
            default = await manager.get_default_persona_v3(self._session() or None)
            if isinstance(default, dict):
                return str(default.get("prompt") or "") or None
            return str(getattr(default, "prompt", "") or "") or None
        except Exception as err:
            logger.warning("[mc-bridge] 取人格失败：%r", err)
            return None

    # ------------------------------------------------------------ QQ → 游戏
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        if not self.config.get("forward_qq_to_game", True):
            return
        umo = event.unified_msg_origin
        session = self._session()
        if session and umo != session:
            return  # 只转发绑定的那个会话，免得全群乱入

        # 记住 bot 和群号：游戏里 @ 人时要靠它查群成员
        try:
            self._bot = getattr(event, "bot", None)
            self._group_id = event.get_group_id() or self._group_id
        except Exception:
            pass

        ats = self._incoming_ats(event)

        # 触发条件只有一个：# 前缀。
        # 注意 @机器人 本身**不算**触发 —— 不然群里 @机器人 说话就被吞掉、没法正常聊天了。
        # （AstrBot 会把开头的 @机器人 从 message_str 里去掉，所以 `@机器人 #你好` 这里
        #   拿到的就是 `#你好`；而 @其他人 会留下 `@昵称(qq)` 文本，得先剥掉。）
        prefix = str(self.config.get("qq_prefix", "#") or "")
        text = self._strip_leading_mentions(event.message_str or "")
        if not prefix or not text.startswith(prefix):
            return
        text = text[len(prefix):].strip()

        # 群里 @ 了谁，就在推给游戏的文本里也写出来（游戏里没法真的 @）
        self_id = str(event.get_self_id() or "")
        head_ats = "".join("@%s " % self._at_name(c) for c in ats if str(getattr(c, "qq", "")) != self_id)
        body = text
        if self.config.get("name_prefix", True):
            sender = ""
            try:
                sender = event.get_sender_name() or ""
            except Exception:
                sender = ""
            if sender:
                body = "%s: %s" % (sender, text) if text else sender
        if head_ats:
            body = head_ats + body

        if self.link is None or not self.link.connected:
            await event.send(event.plain_result("游戏没连上（AstrBot 插件在跑吗？游戏里 #连接 了吗？）"))
            event.stop_event()
            return

        wire = str(self.config.get("game_prefix", "[QQ] ") or "") + body
        if not await self.link.send(wire):
            await event.send(event.plain_result("发送失败（游戏那边可能刚断开）"))
        event.stop_event()

    @staticmethod
    def _strip_leading_mentions(text: str) -> str:
        """剥掉开头的 @别人(qq) 文本。

        AstrBot 会把「非机器人的 @」写进 message_str（形如 ` @昵称(123456) `），
        而开头的 @机器人 会被它自己去掉。这里统一收拾干净，好判断 # 前缀。
        """
        text = (text or "").strip()
        while text.startswith("@"):
            _, _, tail = text.partition(" ")
            text = tail.strip()
        return text

    def _incoming_ats(self, event: AstrMessageEvent):
        """取出消息里的 At 组件。"""
        ats = []
        try:
            for comp in event.get_messages():
                if isinstance(comp, At):
                    ats.append(comp)
        except Exception as err:
            logger.debug("[mc-bridge] 取 At 组件失败：%r", err)
        return ats

    def _at_name(self, comp):
        """At 组件显示成人看的名字：优先 name，其次从成员缓存里找，最后用 QQ 号。"""
        name = str(getattr(comp, "name", "") or "")
        if name:
            return name
        qq = getattr(comp, "qq", "")
        cached = self._member_cache.get(self._group_id)
        if cached:
            for member in cached[1]:
                if str(member.get("user_id")) == str(qq):
                    return str(member.get("card") or member.get("nickname") or qq)
        return str(qq)

    # ------------------------------------------------------------ /mc 管理
    @filter.command_group("mc", alias={"游戏桥"})
    def mc(self):
        """游戏桥管理指令"""
        pass

    @mc.command("bind")
    async def mc_bind(self, event: AstrMessageEvent):
        """把当前会话设为转发目标"""
        self.target_session = event.unified_msg_origin
        self._save_state()
        await event.send(
            event.plain_result(
                "已绑定当前会话：%s\n游戏消息会转发到这里；这个会话里以 %s 开头的消息会推给游戏。"
                % (self.target_session, str(self.config.get("qq_prefix", "#")))
            )
        )

    @mc.command("status")
    async def mc_status(self, event: AstrMessageEvent):
        """看桥的状态"""
        session = self._session() or "（未绑定）"
        cached = self._member_cache.get(self._group_id)
        lines = [
            "游戏连接：%s" % ("已接入" if (self.link and self.link.connected) else "未接入"),
            "监听地址：ws://%s:%s/" % (
                str(self.config.get("host", "127.0.0.1")),
                str(self.config.get("port", 8800)),
            ),
            "目标会话：%s" % session,
            "游戏→QQ：%s" % ("开" if self.config.get("forward_game_to_qq", True) else "关"),
            "QQ→游戏：%s（前缀 %r）" % (
                "开" if self.config.get("forward_qq_to_game", True) else "关",
                str(self.config.get("qq_prefix", "#")),
            ),
            "群成员缓存：%d 人%s" % (
                len(cached[1]) if cached else 0,
                "" if cached else "（还没查过）",
            ),
        ]
        # AI 对话那一路用的模型（就是 AstrBot 当前使用的对话模型）
        try:
            provider = self.context.get_using_provider(umo=self._session() or None)
            if provider is None:
                lines.append("AI 对话：**没有可用的对话模型**（去 AstrBot 里配一个）")
            else:
                pid = type(provider).__name__
                try:
                    meta = provider.meta()
                    pid = getattr(meta, "id", "") or pid
                except Exception:
                    pass
                lines.append("AI 对话：可用（%s）游戏侧前缀 %r"
                             % (pid, str(self.config.get("ai_prefix", "*"))))
                lines.append("AI 记忆：%d 轮（上限 %d 轮，/mc forget 清空）"
                             % (len(self._ai_history) // 2, self._history_rounds()))
        except Exception as err:
            lines.append("AI 对话：取模型出错（%r）" % (err,))
        await event.send(event.plain_result("\n".join(lines)))

    @mc.command("send")
    async def mc_send(self, event: AstrMessageEvent, text: str = ""):
        """手动推一条给游戏：/mc send 文本（也支持 /mc send @某人 文本，只是文本形式）"""
        if not text:
            await event.send(event.plain_result("用法：/mc send 要推给游戏的文本"))
            return
        if self.link is None or not self.link.connected:
            await event.send(event.plain_result("游戏没连上"))
            return
        if not await self.link.send(str(self.config.get("game_prefix", "[QQ] ") or "") + text):
            await event.send(event.plain_result("发送失败"))

    @mc.command("forget")
    async def mc_forget(self, event: AstrMessageEvent):
        """清空游戏 AI 的记忆"""
        count = self._forget()
        await event.send(event.plain_result("已忘掉游戏里那 %d 轮对话" % count))

    @mc.command("persona")
    async def mc_persona(self, event: AstrMessageEvent):
        """看游戏 AI 用的是哪个人设 / 列出可用人格"""
        manual = str(self.config.get("ai_system_prompt", "") or "").strip()
        wanted = str(self.config.get("ai_persona", "") or "").strip()
        lines = ["游戏 AI 的人设来源："]
        if manual:
            lines.append("  ai_system_prompt（手写，%d 字）" % len(manual))
        elif wanted:
            lines.append("  ai_persona = %r" % wanted)
        else:
            lines.append("  AstrBot 默认人格（ai_use_default_persona=%s）"
                         % ("开" if self.config.get("ai_use_default_persona", True) else "关"))

        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            lines.append("拿不到 persona_manager（这个 AstrBot 版本可能不支持）")
        else:
            try:
                personas = await manager.get_all_personas()
                lines.append("可用人格（%d 个）：" % len(personas))
                for item in personas[:20]:
                    lines.append("  - %s" % (getattr(item, "persona_id", "?") or "?"))
                lines.append("填法：插件配置 ai_persona = 上面某个名字")
            except Exception as err:
                lines.append("取人格列表失败：%r" % (err,))
        await event.send(event.plain_result("\n".join(lines)))

    @mc.command("help")
    async def mc_help(self, event: AstrMessageEvent):
        """帮助"""
        await event.send(
            event.plain_result(
                "/mc bind        把当前会话设为转发目标\n"
                "/mc status      看桥的状态（含 AI 模型和群成员缓存）\n"
                "/mc persona     看 / 列出 AI 人格\n"
                "/mc forget      清空游戏 AI 的记忆\n"
                "/mc send 文本    手动推一条给游戏\n"
                "/mc help        这条\n"
                "\n"
                "游戏里： #连接 连上　#文本 发给 QQ　#@某人 文本 就是 @ 他\n"
                "        *文本 和 AI 对话（回复以 [AI] 开头）\n"
                "QQ 里：  #文本 推给游戏（@机器人 不加 # 就是正常聊天，不会被转发）"
            )
        )

    # ------------------------------------------------------------ 小工具
    def _session(self) -> str:
        return self.target_session or str(self.config.get("target_session", "") or "")

    def _state_file(self) -> str:
        if self._state_path:
            return self._state_path
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

    def _load_state(self):
        try:
            path = self._state_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                self.target_session = str(data.get("target_session", "") or "")
        except Exception as err:
            logger.warning("[mc-bridge] 读 state 失败：%r", err)

    def _save_state(self):
        try:
            path = self._state_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                json.dump({"target_session": self.target_session}, fp, ensure_ascii=False, indent=2)
        except Exception as err:
            logger.warning("[mc-bridge] 写 state 失败：%r", err)
