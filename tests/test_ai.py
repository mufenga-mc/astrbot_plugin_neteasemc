#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""AI 对话那一路的离线自测（不用连游戏、不用真调模型）。

用 AstrBot 那个 venv 的 python 跑：
    <实例目录>/venv/Scripts/python.exe tests/test_ai.py

拿的是真的 `McBridge._handle_ai_chat`，只把 link / context / config 换成假的，
所以测的是真逻辑。
"""

import asyncio
import os
import sys
import tempfile

import _bootstrap

_bootstrap.setup()          # 仓库根 + AstrBot core 都加进 sys.path

from main import McBridge  # noqa: E402


class Resp(object):
    def __init__(self, text):
        self.completion_text = text


class StubLink(object):
    connected = True

    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return True


class StubProvider(object):
    def __init__(self, reply="", boom=None):
        self.reply = reply
        self.boom = boom
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.boom:
            raise RuntimeError(self.boom)
        return Resp(self.reply)


class StubPersona(object):
    """模拟 AstrBot 的 Persona（有 persona_id / system_prompt）。"""

    def __init__(self, persona_id, system_prompt):
        self.persona_id = persona_id
        self.system_prompt = system_prompt


class StubPersonaManager(object):
    def __init__(self, by_id=None, all_personas=None, default=None):
        self.by_id = by_id or {}
        self.all_personas = all_personas or []
        self.default = default

    def get_persona_v3_by_id(self, persona_id):
        return self.by_id.get(persona_id)

    async def get_all_personas(self):
        return self.all_personas

    async def get_default_persona_v3(self, umo=None):
        return self.default


class Stub(object):
    """只提供 _handle_ai_chat / _resolve_system_prompt / 记忆 需要的东西。"""

    _handle_ai_chat = McBridge._handle_ai_chat
    _resolve_system_prompt = McBridge._resolve_system_prompt
    _ai_contexts = McBridge._ai_contexts
    _remember = McBridge._remember
    _history_rounds = McBridge._history_rounds
    _forget = McBridge._forget
    _save_ai_history = McBridge._save_ai_history
    _load_ai_history = McBridge._load_ai_history
    _history_file = McBridge._history_file

    def __init__(self, link, provider, config, persona_manager=None, history_path=""):
        self.link = link
        self._provider = provider
        self.config = config
        self.context = self
        self.persona_manager = persona_manager
        self._ai_history = []
        self._history_path = history_path

    def get_using_provider(self, umo=None):
        return self._provider

    def _session(self):
        return ""


def make(config=None, provider=None, link=None, persona_manager=None, history_path=""):
    cfg = {"ai_label": "[AI] ", "ai_prefix": "*", "ai_system_prompt": "", "ai_history_rounds": 6}
    cfg.update(config or {})
    link = link or StubLink()
    if not history_path:
        # 默认塞到临时目录：别让测试在插件目录里写 ai_history.json
        history_path = os.path.join(tempfile.mkdtemp(prefix="mc-ai-test-"), "ai_history.json")
    return Stub(link, provider, cfg, persona_manager, history_path), link


async def main():
    failed = []

    # ① 正常多行回复 → 拆成多条，每条带 [AI]
    stub, link = make(provider=StubProvider(reply="第一行\n\n第二行\n"))
    await stub._handle_ai_chat("你好")
    got = link.sent
    if got != ["[AI] 第一行", "[AI] 第二行"]:
        failed.append("多行拆分：%r" % (got,))
    print("%s 多行回复 -> %r" % ("OK " if not failed else "BAD", got))

    # ② 传给模型的参数对不对
    calls = stub._provider.calls
    if not calls or calls[0].get("prompt") != "你好":
        failed.append("调用参数：%r" % (calls,))
    print("OK  调用参数 -> prompt=%r" % (calls[0]["prompt"],))

    # ③ 没有可用模型
    stub, link = make(provider=None)
    await stub._handle_ai_chat("你好")
    if not link.sent or "没有可用的对话模型" not in link.sent[0]:
        failed.append("无模型分支：%r" % (link.sent,))
    print("OK  无模型 -> %r" % (link.sent,))

    # ④ 模型抛异常
    stub, link = make(provider=StubProvider(boom="timeout"))
    await stub._handle_ai_chat("你好")
    if not link.sent or "调用出错" not in link.sent[0]:
        failed.append("异常分支：%r" % (link.sent,))
    print("OK  异常 -> %r" % (link.sent,))

    # ⑤ 空回复
    stub, link = make(provider=StubProvider(reply="   "))
    await stub._handle_ai_chat("你好")
    if not link.sent or "没返回内容" not in link.sent[0]:
        failed.append("空回复分支：%r" % (link.sent,))
    print("OK  空回复 -> %r" % (link.sent,))

    # ⑥ 空提问
    stub, link = make(provider=StubProvider(reply="x"))
    await stub._handle_ai_chat("")
    if not link.sent or "你想问什么" not in link.sent[0]:
        failed.append("空提问分支：%r" % (link.sent,))
    print("OK  空提问 -> %r" % (link.sent,))

    # ⑦ 开了「在想…」提示
    stub, link = make(config={"ai_thinking_notice": True}, provider=StubProvider(reply="好"))
    await stub._handle_ai_chat("你好")
    if link.sent[0] != "[AI] （在想…）" or link.sent[-1] != "[AI] 好":
        failed.append("在想提示：%r" % (link.sent,))
    print("OK  在想提示 -> %r" % (link.sent,))

    if failed:
        print("[ai-test] 失败：%s" % failed)
        return 1

    # ⑧ 人设：优先级 / 人格解析 / 默认人格 / 开关
    manager = StubPersonaManager(
        by_id={"浅葱": {"prompt": "你是浅葱", "name": "浅葱"}},
        all_personas=[StubPersona("只有全量列表里才有", "来自全量列表的人设")],
        default={"prompt": "默认人格", "name": "default"},
    )
    persona_cases = [
        # (说明, 配置, 期望传给模型的 system_prompt)
        ("手写人设优先", {"ai_system_prompt": "你是猫娘", "ai_persona": "浅葱"}, "你是猫娘"),
        ("按 persona_id 取人格", {"ai_persona": "浅葱"}, "你是浅葱"),
        ("按全量列表兜底找", {"ai_persona": "只有全量列表里才有"}, "来自全量列表的人设"),
        ("找不到就用默认人格", {"ai_persona": "不存在的人格"}, "默认人格"),
        ("没指定就用默认人格", {}, "默认人格"),
        ("关掉默认人格 = 裸模型", {"ai_use_default_persona": False}, None),
    ]
    for desc, cfg, want in persona_cases:
        stub, link = make(config=cfg, provider=StubProvider(reply="好"), persona_manager=manager)
        await stub._handle_ai_chat("你好")
        got = stub._provider.calls[0].get("system_prompt")
        ok = got == want
        print("%s 人设 %-22s -> %r" % ("OK " if ok else "BAD", desc, got))
        if not ok:
            failed.append("persona:%s" % desc)

    # ⑨ 拿不到 persona_manager 时不能炸
    stub, link = make(provider=StubProvider(reply="好"))
    got = await stub._resolve_system_prompt()
    if got is not None:
        failed.append("无人格管理器：%r" % (got,))
    print("%s 无人格管理器 -> %r" % ("OK " if got is None else "BAD", got))

    if failed:
        print("[ai-test] 失败：%s" % failed)
        return 1

    # ⑩ 记忆：首轮不该有上下文，第二轮要带上第一轮
    tmpdir = tempfile.mkdtemp(prefix="mc-ai-test-")
    hist = os.path.join(tmpdir, "ai_history.json")
    provider = StubProvider(reply="第一答")
    stub, link = make(provider=provider, history_path=hist)
    await stub._handle_ai_chat("第一问")
    first = provider.calls[0].get("contexts")
    if first:
        failed.append("首轮不该有上下文：%r" % (first,))
    print("%s 首轮 contexts -> %r" % ("OK " if not first else "BAD", first))

    provider.reply = "第二答"
    await stub._handle_ai_chat("第二问")
    got = provider.calls[1].get("contexts")
    want = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
    ]
    ok = got == want
    if not ok:
        failed.append("第二轮上下文：%r" % (got,))
    print("%s 第二轮 contexts -> %r" % ("OK " if ok else "BAD", got))

    # ⑪ 落盘后重启也不失忆
    stub2, _ = make(provider=StubProvider(reply="x"), history_path=hist)
    stub2._load_ai_history()
    ok = len(stub2._ai_history) == 4
    if not ok:
        failed.append("落盘恢复：%r" % (stub2._ai_history,))
    print("%s 落盘后恢复 %d 条" % ("OK " if ok else "BAD", len(stub2._ai_history)))

    # ⑫ 轮数上限（2 轮 = 4 条）
    stub3, _ = make(config={"ai_history_rounds": 2}, provider=StubProvider(reply="r"),
                    history_path=os.path.join(tmpdir, "h2.json"))
    for i in range(4):
        await stub3._handle_ai_chat("问%d" % i)
    last = stub3._provider.calls[-1].get("contexts") or []
    ok = len(stub3._ai_history) == 4 and len(last) == 4 and last[0]["content"] == "问1"
    if not ok:
        failed.append("轮数上限：%r / %r" % (stub3._ai_history, last))
    print("%s 上限 2 轮 -> 记忆 %d 条，上下文 %d 条，最旧的是 %r"
          % ("OK " if ok else "BAD", len(stub3._ai_history), len(last),
             last[0]["content"] if last else None))

    # ⑬ 0 轮 = 完全不记
    stub4, _ = make(config={"ai_history_rounds": 0}, provider=StubProvider(reply="r"),
                    history_path=os.path.join(tmpdir, "h3.json"))
    await stub4._handle_ai_chat("问")
    await stub4._handle_ai_chat("再问")
    ok = (not stub4._ai_history) and (not stub4._provider.calls[1].get("contexts"))
    if not ok:
        failed.append("0 轮仍记忆：%r / %r"
                      % (stub4._ai_history, stub4._provider.calls[1].get("contexts")))
    print("%s 0 轮不记 -> %r / %r"
          % ("OK " if ok else "BAD", stub4._ai_history,
             stub4._provider.calls[1].get("contexts")))

    # ⑭ forget
    n = stub._forget()
    ok = n == 2 and not stub._ai_history
    if not ok:
        failed.append("forget：%d / %r" % (n, stub._ai_history))
    print("%s forget 清掉 %d 轮" % ("OK " if ok else "BAD", n))

    if failed:
        print("[ai-test] 失败：%s" % failed)
        return 1
    print("[ai-test] 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
