"""AstrBot adapter for memory-core.

- uses the being's identity documents (identity/*.md) as its persona
- records every message it can see (and its own replies) as episodes
- recalls relevant memories before each model call and injects them
- periodically consolidates episodes into memories with AstrBot's own chat model
- writes a diary every day and refreshes the being's self-image from it
- gives the model `memory_recall` / `memory_remember` tools
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import TextPart

from memory_core import Audience, Episode, Memory, MemoryCore
from memory_core import timeutil
from memory_core.reflect import current_day, day_window, diary_for, update_self_image, write_diary

from .logic import (
    IDENTITY_DOCS,
    OWNER_ALIAS,
    SELF_ALIAS,
    actor_alias,
    default_scope,
    identity_block,
    is_command,
    is_owner_private,
    memory_block,
    memory_guide,
    owner_spaces,
    space_of,
    speaker_note,
    strip_framework_persona,
)

try:  # the persona AstrBot's web chat uses when none is selected
    from astrbot.core.astr_main_agent_resources import CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT as _WEBCHAT_DEFAULT
except Exception:  # noqa: BLE001 - internal name, may move between AstrBot versions
    _WEBCHAT_DEFAULT = ""

PLUGIN_NAME = "astrbot_plugin_memory_core"


class AstrBotLLM:
    """memory-core's LLM protocol on top of AstrBot's provider (called from a worker thread)."""

    def __init__(self, context, provider_id: str, loop: asyncio.AbstractEventLoop, timeout: float = 300) -> None:
        self.context = context
        self.provider_id = provider_id
        self.model_id = f"astrbot:{provider_id}"
        self.loop = loop
        self.timeout = timeout

    def complete(self, system: str, user: str) -> str:
        coro = self.context.llm_generate(chat_provider_id=self.provider_id, prompt=user, system_prompt=system)
        resp = asyncio.run_coroutine_threadsafe(coro, self.loop).result(self.timeout)
        return resp.completion_text or ""


class MemoryCorePlugin(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg = config or {}
        root = (self.cfg.get("soul_dir") or "").strip()
        self.root = Path(root) if root else Path(star.StarTools.get_data_dir(PLUGIN_NAME)) / "soul"
        self.owner_ids = [str(x) for x in self.cfg.get("owner_ids", []) if str(x).strip()]
        self.owner_name = (self.cfg.get("owner_name") or "主人").strip()
        # what the being calls its owner in prompts (e.g. 爸爸); defaults to owner_name
        self.owner_title = (self.cfg.get("owner_title") or "").strip() or self.owner_name
        self.core: MemoryCore | None = None
        self._task: asyncio.Task | None = None
        self._diary_task: asyncio.Task | None = None
        self._consolidating = asyncio.Lock()
        self._reflecting = asyncio.Lock()
        self._known_names: dict[str, str] = {}

    # ---------------------------------------------------------------- lifecycle

    async def initialize(self) -> None:
        self.core = await asyncio.to_thread(MemoryCore.open, self.root)
        self_name = (self.cfg.get("self_name") or "我").strip()
        me = await asyncio.to_thread(self.core.ensure_entity, SELF_ALIAS, name=self_name)
        if me.name != self_name:
            await asyncio.to_thread(self.core.rename_entity, me.id, self_name)
        stats = self.core.stats()
        logger.info(f"[memory-core] opened {self.root}: {stats['episodes']} episodes, "
                    f"{stats['memories']} memories, embedder={stats['embedder']}")
        if not self.owner_ids:
            logger.warning("[memory-core] owner_ids is empty: no conversation is treated as the owner's")
        if int(self.cfg.get("consolidate_interval_minutes", 30)) > 0:
            self._task = asyncio.create_task(self._consolidate_loop())
        if self.cfg.get("diary_enabled", True):
            self._diary_task = asyncio.create_task(self._diary_loop())

    async def terminate(self) -> None:
        for task in (self._task, self._diary_task):
            if task:
                task.cancel()
        if self.core:
            await asyncio.to_thread(self.core.close)

    # ---------------------------------------------------------------- helpers

    def _where(self, event: AstrMessageEvent) -> tuple[str, bool, bool]:
        is_group = bool(event.get_group_id())
        sender = str(event.get_sender_id())
        space = space_of(event.get_platform_name(), is_group, event.get_group_id(), sender)
        return space, is_group, is_owner_private(is_group, sender, self.owner_ids)

    def _ensure_actor(self, event: AstrMessageEvent) -> str:
        alias = actor_alias(event.get_platform_name(), str(event.get_sender_id()))
        if str(event.get_sender_id()) in self.owner_ids:
            # The owner is one person across platforms: one entity, every account attached as an alias.
            if alias not in self._known_names:
                self.core.ensure_entity(OWNER_ALIAS, name=self.owner_name, aliases=(alias,))
                self._known_names[alias] = self.owner_name
            return OWNER_ALIAS
        name = (event.get_sender_name() or "").strip() or alias
        if self._known_names.get(alias) != name:
            extra = (name,) if name != alias else ()
            self.core.ensure_entity(alias, name=name, aliases=extra)
            self._known_names[alias] = name
        return alias

    # ---------------------------------------------------------------- recording

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_message(self, event: AstrMessageEvent):
        """Record every visible message (does not make the bot reply)."""
        if self.core is None or not self.cfg.get("record_messages", True):
            return
        text = (event.get_message_outline() or "").strip()
        if not text or is_command(text, self.cfg.get("skip_prefixes", ["/"])):
            return
        space, is_group, _ = self._where(event)
        if is_group and not self.cfg.get("record_group_messages", True):
            return
        try:
            def write():
                actor = self._ensure_actor(event)
                self.core.record(Episode(
                    kind="message_in", content=text, space=space, source=event.get_platform_name() or "astrbot",
                    actor=actor, meta={"message_id": str(getattr(event.message_obj, "message_id", "") or "")}))
            await asyncio.to_thread(write)
        except Exception as exc:
            logger.warning(f"[memory-core] failed to record message: {exc}")

    @filter.on_llm_response()
    async def on_reply(self, event: AstrMessageEvent, resp: LLMResponse):
        """Record what the bot said. Fires once per finished model answer, streaming or not
        (after_message_sent is skipped for streamed replies, e.g. in WebChat). Command replies are not
        model answers, so they are not recorded."""
        if self.core is None or not self.cfg.get("record_messages", True):
            return
        text = (getattr(resp, "completion_text", "") or "").strip()
        if not text:
            return
        space, _, _ = self._where(event)
        try:
            await asyncio.to_thread(self.core.record, Episode(
                kind="message_out", content=text, space=space, source=event.get_platform_name() or "astrbot",
                actor=SELF_ALIAS))
        except Exception as exc:
            logger.warning(f"[memory-core] failed to record reply: {exc}")

    # ---------------------------------------------------------------- recall

    def _framework_persona_prompt(self, event: AstrMessageEvent) -> str | None:
        persona_id = event.get_extra("_trace_persona_id")
        if not persona_id:
            return None
        try:
            for item in self.context.persona_manager.personas_v3:
                if item.get("name") == persona_id:
                    return item.get("prompt") or None
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[memory-core] cannot read AstrBot personas: {exc}")
        return None

    def _apply_identity(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """The being's identity documents go first in the system prompt (stable, so cached);
        who is speaking goes last (it changes per sender)."""
        docs = [(tag, self.core.identity(name)) for name, tag in IDENTITY_DOCS]
        block = identity_block(docs, guide=memory_guide(self.owner_title))
        if not block:
            return
        system = req.system_prompt or ""
        if self.cfg.get("replace_framework_persona", True):
            system = strip_framework_persona(system, self._framework_persona_prompt(event), (_WEBCHAT_DEFAULT,))
        sender = str(event.get_sender_id())
        note = speaker_note(sender in self.owner_ids, self.owner_title, (event.get_sender_name() or "").strip(),
                            bool(event.get_group_id()))
        req.system_prompt = f"{block}\n\n{system.strip()}\n\n{note}\n"

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        """Identity into the system prompt; relevant memories next to the user's message."""
        if self.core is None:
            return
        if self.cfg.get("inject_identity", True):
            try:
                await asyncio.to_thread(self._apply_identity, event, req)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[memory-core] identity injection failed: {exc}")
        if not self.cfg.get("inject_memories", True):
            return
        query = (req.prompt or event.get_message_str() or "").strip()
        if not query:
            return
        space, _, owner = self._where(event)
        try:
            hits = await asyncio.to_thread(self.core.recall, query, Audience(space, owner_present=owner),
                                           int(self.cfg.get("inject_top_k", 6)))
        except Exception as exc:
            logger.warning(f"[memory-core] recall failed: {exc}")
            return
        block = memory_block([h.memory.text for h in hits], int(self.cfg.get("inject_max_chars", 1200)))
        if block:
            req.extra_user_content_parts.append(TextPart(text=block))
            logger.debug(f"[memory-core] injected {len(hits)} memories for {space}")

    # ---------------------------------------------------------------- tools for the model

    @filter.llm_tool(name="memory_recall")
    async def tool_recall(self, event: AstrMessageEvent, query: str):
        """Search your long-term memory for things you may have forgotten (people, past events, slang).

        Args:
            query(string): what to look for
        """
        if self.core is None:
            return "memory is not available"
        space, _, owner = self._where(event)
        hits = await asyncio.to_thread(self.core.recall, query, Audience(space, owner_present=owner), 8)
        return "\n".join(f"- {h.memory.text}" for h in hits) or "（没有相关记忆）"

    @filter.llm_tool(name="memory_remember")
    async def tool_remember(self, event: AstrMessageEvent, text: str, kind: str = "fact"):
        """Remember something important for the long term, e.g. when someone asks you to remember it.

        Args:
            text(string): one self-contained sentence, using names instead of pronouns
            kind(string): one of fact, event, relation, preference, lexicon
        """
        if self.core is None:
            return "memory is not available"
        if kind not in ("fact", "event", "relation", "preference", "lexicon"):
            kind = "fact"
        space, _, owner = self._where(event)
        memory = Memory(kind=kind, text=text, scope=default_scope(space, owner), importance=0.7,
                        derived_by="astrbot-tool")
        await asyncio.to_thread(self.core.add_memory, memory)
        return "记住了"

    # ---------------------------------------------------------------- consolidation

    def _provider_id(self) -> str | None:
        configured = (self.cfg.get("llm_provider_id") or "").strip()
        if configured:
            return configured
        provider = self.context.get_using_provider()
        return provider.meta().id if provider else None

    async def consolidate_now(self, *, force: bool = False) -> dict:
        """force=True consolidates every chat with at least one new message (manual command)."""
        provider_id = self._provider_id()
        if not provider_id:
            raise RuntimeError("no chat provider available for consolidation")
        llm = AstrBotLLM(self.context, provider_id, asyncio.get_running_loop())
        async with self._consolidating:
            def run():
                spaces = self.core.store.space_heads().keys()
                return self.core.consolidate(llm, owner_spaces=owner_spaces(spaces, self.owner_ids),
                                             min_new=1 if force else int(self.cfg.get("consolidate_min_new", 10)))
            started = time.monotonic()
            report = await asyncio.to_thread(run)
        summary = {"episodes": report.episodes, "added": len(report.added), "updated": len(report.updated),
                   "rejected": len(report.rejected), "seconds": round(time.monotonic() - started, 1)}
        if report.episodes:
            logger.info(f"[memory-core] consolidated: {summary}")
        return summary

    async def _consolidate_loop(self) -> None:
        interval = max(1, int(self.cfg.get("consolidate_interval_minutes", 30))) * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self.consolidate_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[memory-core] consolidation failed (will retry next round): {exc}")

    # ---------------------------------------------------------------- diary and self-image

    def _persona(self, *names: str) -> str:
        tags = dict(IDENTITY_DOCS)
        return identity_block([(tags[n], self.core.identity(n)) for n in names], guide="")

    async def reflect_day(self, day, *, force: bool = False) -> tuple[str | None, str | None]:
        """Write the diary of `day`, then refresh the self-image. Returns (diary text, self-image)."""
        provider_id = self._provider_id()
        if not provider_id:
            raise RuntimeError("no chat provider available for the diary")
        llm = AstrBotLLM(self.context, provider_id, asyncio.get_running_loop())
        hour = int(self.cfg.get("diary_hour", 4))
        start, end = day_window(day, hour)
        async with self._reflecting:
            def run():
                if not self.core.episodes(until=end, limit=1):
                    return None, None  # not born yet that day
                spaces = self.core.store.space_heads().keys()
                result = write_diary(self.core, llm, day, start, end,
                                     persona=self._persona("self", "self-image", "voice"),
                                     owner_spaces=owner_spaces(spaces, self.owner_ids),
                                     write_quiet_days=bool(self.cfg.get("diary_quiet_days", True)), force=force,
                                     owner_title=self.owner_title)
                if result.memory is None or result.skipped:
                    return (result.memory.text if result.memory else None), None
                image = update_self_image(self.core, llm, day, persona=self._persona("self", "voice"),
                                          days=int(self.cfg.get("self_image_days", 7)),
                                          owner_title=self.owner_title)
                return result.memory.text, image
            diary, image = await asyncio.to_thread(run)
        if diary:
            logger.info(f"[memory-core] diary for {day} ready; self-image {'updated' if image else 'unchanged'}")
        return diary, image

    async def _diary_loop(self) -> None:
        hour = int(self.cfg.get("diary_hour", 4))
        while True:
            try:
                yesterday = current_day(timeutil.now(), hour) - timedelta(days=1)  # a finished day
                if await asyncio.to_thread(diary_for, self.core, yesterday) is None:
                    await self.reflect_day(yesterday)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[memory-core] diary failed (will retry later): {exc}")
            await asyncio.sleep(600)

    # ---------------------------------------------------------------- admin commands

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_diary")
    async def cmd_diary(self, event: AstrMessageEvent):
        """让她现在就写今天的日记（会覆盖今天已写的）"""
        day = current_day(timeutil.now(), int(self.cfg.get("diary_hour", 4)))
        try:
            diary, image = await self.reflect_day(day, force=True)
        except Exception as exc:
            yield event.plain_result(f"写日记失败：{exc}")
            return
        if not diary:
            yield event.plain_result("今天还没有经历，没写日记")
            return
        tail = f"\n\n——我眼中的自己（已更新）——\n{image}" if image else ""
        yield event.plain_result(f"【{day.isoformat()} 的日记】\n{diary}{tail}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """memory-core 状态"""
        stats = await asyncio.to_thread(self.core.stats)
        pending = await asyncio.to_thread(self.core.pending_consolidation)
        yield event.plain_result(
            f"经历 {stats['episodes']} 条，记忆 {stats['memories']} 条，实体 {stats['entities']} 个，"
            f"向量 {stats['embeddings']} 个（{stats['embedder'] or '未配置嵌入模型'}）。\n"
            f"待整理：{sum(pending.values())} 条，来自 {len(pending)} 个场所。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_consolidate")
    async def cmd_consolidate(self, event: AstrMessageEvent):
        """立即整理记忆"""
        try:
            summary = await self.consolidate_now(force=True)
        except Exception as exc:
            yield event.plain_result(f"整理失败：{exc}")
            return
        yield event.plain_result(f"整理完成：处理经历 {summary['episodes']} 条，新增 {summary['added']} 条，"
                                 f"更新 {summary['updated']} 条，丢弃 {summary['rejected']} 条，"
                                 f"用时 {summary['seconds']} 秒")
