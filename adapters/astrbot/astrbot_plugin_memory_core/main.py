"""AstrBot adapter for memory-core.

- uses the being's identity documents (identity/*.md) as its persona
- records every message it can see (and its own replies) as episodes
- recalls relevant memories before each model call and injects them
- periodically consolidates episodes into memories with AstrBot's own chat model
- writes a diary every day and refreshes the being's self-image from it
- memory metabolism: faded memories are archived, duplicates merged; the owner can hard-forget;
  the being can only propose changes to its core identity, the owner approves
- gives the model `memory_recall` / `memory_remember` tools
- sees pictures through a vision model (cached by content) and remembers what was in them
- tells the being what it can do (collected from the installed plugins) and lets it notice new abilities
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.components import Image, Reply
from astrbot.core.agent.message import TextPart

from memory_core import Audience, Episode, Memory, MemoryCore
from memory_core import timeutil
from memory_core.abilities import Ability, abilities_block, gained_note, lost_note, notice
from memory_core.consolidate import due_spaces, remember
from memory_core.metabolism import fade, merge_duplicates
from memory_core.now import describe, mood_due, snapshot, update_mood
from memory_core.proposals import proposals, propose, resolve
from memory_core.reflect import current_day, day_window, diary_for, update_self_image, write_diary
from memory_core.vision import Seen, caption_block, look, outline

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
    owner_console,
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


def _no_urls(text) -> str:
    """Errors about pictures can carry their URL, and some platforms put a token in it (Telegram)."""
    return re.sub(r"https?://\S+", "<url>", str(text))


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
        self.console_platforms = [str(x) for x in self.cfg.get("owner_command_platforms", ["webchat"])]
        self.owner_name = (self.cfg.get("owner_name") or "主人").strip()
        # what the being calls its owner in prompts (e.g. 爸爸); defaults to owner_name
        self.owner_title = (self.cfg.get("owner_title") or "").strip() or self.owner_name
        self.core: MemoryCore | None = None
        self._task: asyncio.Task | None = None
        self._diary_task: asyncio.Task | None = None
        self._consolidating = asyncio.Lock()
        self._reflecting = asyncio.Lock()
        self._known_names: dict[str, str] = {}
        self._forget_candidates: dict[str, list[str]] = {}  # sender -> memory ids listed by /mem_forget

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
        if self.cfg.get("diary_enabled", True) or self.cfg.get("tidy_enabled", True):
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

    def _console_denied(self, event: AstrMessageEvent) -> bool:
        """The memory commands change or reveal private memory: only the owner, in a private chat, on a
        platform listed in owner_command_platforms. Anyone else gets no answer at all (and no LLM reply)."""
        is_group = bool(event.get_group_id())
        if owner_console(event.get_platform_name(), is_group, str(event.get_sender_id()), self.owner_ids,
                         self.console_platforms):
            return False
        logger.warning(f"[memory-core] refused {event.get_message_str()!r} from {event.get_platform_name()}:"
                       f"{event.get_sender_id()} (group={is_group})")
        event.stop_event()
        return True

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
        """Record every visible message (does not make the bot reply). Pictures are looked at first, so the
        memory holds what was in them, not just "[图片]"."""
        if self.core is None:
            return
        text = (event.get_message_outline() or "").strip()
        if not text or is_command(text, self.cfg.get("skip_prefixes", ["/"])):
            return
        space, is_group, owner = self._where(event)
        seen = await self._see(event, owner)
        if seen:
            text = outline(text, seen)
        if not self.cfg.get("record_messages", True):
            return
        if is_group and not self.cfg.get("record_group_messages", True):
            return
        meta = {"message_id": str(getattr(event.message_obj, "message_id", "") or "")}
        if seen:
            meta["images"] = [{"sha": s.sha, "model": s.model, "saved": s.saved} if s else None for s in seen]
        try:
            def write():
                actor = self._ensure_actor(event)
                self.core.record(Episode(
                    kind="message_in", content=text, space=space, source=event.get_platform_name() or "astrbot",
                    actor=actor, meta=meta))
            await asyncio.to_thread(write)
        except Exception as exc:
            logger.warning(f"[memory-core] failed to record message: {exc}")

    # ---------------------------------------------------------------- seeing pictures

    def _viewers(self, loop: asyncio.AbstractEventLoop) -> list:
        """The vision providers in vision_providers, in order (a later one is used when an earlier one fails)."""
        timeout = float(self.cfg.get("vision_timeout", 120))
        viewers = []
        for pid in self.cfg.get("vision_providers", []) or []:
            pid = str(pid).strip()
            provider = self.context.get_provider_by_id(pid) if pid else None
            if provider is None:
                continue

            def see(jpeg: bytes, prompt: str, provider=provider) -> str:
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                    f.write(jpeg)
                    path = f.name
                try:
                    coro = provider.text_chat(prompt=prompt, image_urls=[path])
                    resp = asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
                    return resp.completion_text or ""
                finally:
                    Path(path).unlink(missing_ok=True)

            viewers.append((pid, see))
        return viewers

    @staticmethod
    def _pictures(event: AstrMessageEvent) -> list:
        """Pictures in the message, then pictures in the message it quotes."""
        chain = list(getattr(event.message_obj, "message", None) or [])
        own = [c for c in chain if isinstance(c, Image)]
        quoted = [c for r in chain if isinstance(r, Reply) for c in (r.chain or []) if isinstance(c, Image)]
        return own + quoted

    async def _see(self, event: AstrMessageEvent, owner: bool) -> list[Seen | None]:
        """Look at the pictures in this message (once each; the same picture is remembered by its content).
        Saved on the event for the reply. Never raises."""
        if not self.cfg.get("vision_enabled", True) or not self.cfg.get("vision_providers"):
            return []
        pictures = self._pictures(event)
        if not pictures:
            return []
        own = sum(1 for c in getattr(event.message_obj, "message", None) or [] if isinstance(c, Image))
        pictures = pictures[: max(1, int(self.cfg.get("vision_max_images", 4)))]
        keep = str(self.cfg.get("vision_keep_pictures", "owner"))
        save = keep == "all" or (keep == "owner" and owner)
        viewers = self._viewers(asyncio.get_running_loop())
        seen: list[Seen | None] = []
        for i, comp in enumerate(pictures):
            try:
                data = Path(await comp.convert_to_file_path()).read_bytes()
                errors: list[str] = []
                s = await asyncio.to_thread(look, self.core, data, viewers, save=save and i < own, errors=errors)
                if s is None:
                    logger.warning(f"[memory-core] could not see a picture: "
                                   f"{_no_urls('; '.join(errors)) or 'no vision provider'}")
            except Exception as exc:  # noqa: BLE001 - a picture she cannot see is not an error for the chat
                logger.warning(f"[memory-core] could not read a picture: {_no_urls(exc)}")
                s = None
            seen.append(s)
        event.set_extra("memory_core_seen", seen)
        logger.info(f"[memory-core] saw {sum(1 for s in seen if s)}/{len(seen)} pictures "
                    f"({sum(1 for s in seen if s and s.cached)} remembered)")
        return seen[:own] if own else seen

    def _main_sees_pictures(self, event: AstrMessageEvent) -> bool:
        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
            modalities = provider.provider_config.get("modalities") if provider else None
            return isinstance(modalities, list) and "image" in modalities
        except Exception:  # noqa: BLE001
            return False

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
        guide = memory_guide(self.owner_title, int(self.cfg.get("diary_hour", 4)),
                             diary=bool(self.cfg.get("diary_enabled", True)),
                             tidy=bool(self.cfg.get("tidy_enabled", True)))
        block = identity_block(docs, guide=guide)
        if not block:
            return
        if self.cfg.get("abilities_enabled", True):
            try:
                can = self._abilities_block(event)
                if can:
                    block = f"{block}\n\n{can}"
            except Exception as exc:  # noqa: BLE001 - never block a reply
                logger.warning(f"[memory-core] abilities failed: {exc}")
        system = req.system_prompt or ""
        if self.cfg.get("replace_framework_persona", True):
            system = strip_framework_persona(system, self._framework_persona_prompt(event), (_WEBCHAT_DEFAULT,))
        sender = str(event.get_sender_id())
        note = speaker_note(sender in self.owner_ids, self.owner_title, (event.get_sender_name() or "").strip(),
                            bool(event.get_group_id()))
        now_note = ""
        if self.cfg.get("inner_state", True):
            try:
                _, _, owner = self._where(event)
                now_note = describe(self._snapshot(), owner_present=owner, owner_title=self.owner_title) + "\n\n"
            except Exception as exc:  # noqa: BLE001 - never block a reply
                logger.warning(f"[memory-core] inner state failed: {exc}")
        req.system_prompt = f"{block}\n\n{system.strip()}\n\n{now_note}{note}\n"

    # ---------------------------------------------------------------- what she can do

    def being_abilities(self, owner: str) -> list[Ability]:
        """This plugin's own abilities. Other plugins offer theirs with a method of the same name."""
        out = [Ability("memory-core:memory", "记事和翻找记忆",
                       "聊过的事会自己记下来；想不起来用 memory_recall 翻找，要紧的事用 memory_remember 记牢")]
        if self.cfg.get("diary_enabled", True):
            out.append(Ability("memory-core:diary", "写日记",
                               f"每天凌晨{int(self.cfg.get('diary_hour', 4))}点左右自动回顾前一天写一篇，只有{owner}能看"))
        if self.cfg.get("vision_enabled", True) and any(
                self.context.get_provider_by_id(str(p).strip()) for p in self.cfg.get("vision_providers", []) or []):
            out.append(Ability("memory-core:vision", "看图",
                               "别人发来的图片（包括引用的消息里的）会先被讲成一段话给你，你就知道图里有什么；"
                               "动图只看第一帧，看不清的会告诉你，别编"))
        out.append(Ability("memory-core:identity", "提议改自己的设定",
                           f"真想改核心身份的某一点（比如给自己取了名字），和{owner}私聊时用 identity_propose 提议，"
                           f"等{owner}同意"))
        return out

    def _abilities(self) -> list[Ability]:
        """Every active plugin's abilities: its being_abilities() if it has one, otherwise (a plugin made for
        anyone) its description and the tools it gives the model."""
        owner = self.owner_title
        out = list(self.being_abilities(owner))
        tools: dict[str, list] = {}
        try:
            for tool in self.context.get_llm_tool_manager().func_list:
                if getattr(tool, "active", True) and getattr(tool, "handler_module_path", None):
                    tools.setdefault(tool.handler_module_path, []).append(tool)
        except Exception:  # noqa: BLE001
            pass
        for meta in self.context.get_all_stars():
            plugin = getattr(meta, "star_cls", None)
            if plugin is None or plugin is self or not getattr(meta, "activated", True) or meta.reserved:
                continue
            own = getattr(plugin, "being_abilities", None)
            if callable(own):
                try:
                    for a in own(owner) or []:
                        if isinstance(a, Ability):
                            out.append(a)
                        elif isinstance(a, (tuple, list)) and len(a) == 3:  # plugins need not import memory-core
                            out.append(Ability(*(str(x) for x in a)))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[memory-core] {meta.name}.being_abilities failed: {exc}")
                continue
            mine = tools.get(meta.module_path or "", [])
            if not mine:
                continue  # changes how she behaves, not what she can do
            names = "、".join(t.name for t in mine)
            desc = (meta.short_desc or meta.desc or "").strip().splitlines()
            label = meta.display_name or meta.name
            text = f"{desc[0]}（工具：{names}）" if desc else f"工具：{names}"
            out.append(Ability(f"plugin:{meta.name}", f"用「{label}」", text))
        return out

    def _abilities_block(self, event: AstrMessageEvent) -> str:
        """The list for the prompt. In the owner's private chat she also notices what changed (once), as a
        thought in that chat, so it reaches her memory and her diary."""
        current = self._abilities()
        space, _, owner = self._where(event)
        noticed = notice(self.core, current, fresh_hours=float(self.cfg.get("abilities_fresh_hours", 24)),
                         update=owner)
        if owner:
            for a, note in [(a, gained_note(a)) for a in noticed.gained] + [(a, lost_note(a)) for a in noticed.lost]:
                self.core.record(Episode(kind="thought", content=note, space=space, source="memory-core",
                                         actor=SELF_ALIAS, meta={"ability": a.key}))
                logger.info(f"[memory-core] she noticed: {note}")
        return abilities_block(current, noticed, self.owner_title)

    def _owner_spaces(self) -> list[str]:
        return owner_spaces(self.core.store.space_heads().keys(), self.owner_ids)

    def _snapshot(self):
        return snapshot(self.core, owner_spaces=self._owner_spaces(),
                        day_boundary_hour=int(self.cfg.get("diary_hour", 4)),
                        longing_half_hours=float(self.cfg.get("longing_half_hours", 12)),
                        sleep_hour=float(self.cfg.get("sleep_hour", 23)), wake_hour=float(self.cfg.get("wake_hour", 9)),
                        jitter_ratio=float(self.cfg.get("timing_jitter", 0.2)))

    async def update_mood_if_due(self) -> bool:
        """After a talk (or an outing) has been quiet for mood_idle_minutes, judge how she feels now."""
        provider_id = self._provider_id()
        if not provider_id:
            return False
        newest = await asyncio.to_thread(mood_due, self.core, self._owner_spaces(), timeutil.now(),
                                         float(self.cfg.get("mood_idle_minutes", 30)))
        if newest is None:
            return False
        llm = AstrBotLLM(self.context, provider_id, asyncio.get_running_loop(), timeout=120)
        mood = await asyncio.to_thread(update_mood, self.core, llm, owner_spaces=self._owner_spaces(),
                                       persona=self._persona("self", "self-image"), owner_title=self.owner_title,
                                       until=newest)
        logger.info(f"[memory-core] mood: {mood.label} ({mood.value:+.1f})")
        return True

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
        seen = event.get_extra("memory_core_seen") or []
        if seen and req.image_urls and not self._main_sees_pictures(event):
            # the chat model is text-only: it gets what she saw instead of the pictures
            req.image_urls = []
            req.extra_user_content_parts.append(TextPart(text=caption_block(seen)))
        if not self.cfg.get("inject_memories", True):
            return
        query = (event.get_message_str() or "").strip() or " ".join(s.text for s in seen if s)
        query = query or (req.prompt or "").strip()
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
        Also finds faded memories; finding one brings it back.

        Args:
            query(string): what to look for
        """
        if self.core is None:
            return "memory is not available"
        space, _, owner = self._where(event)
        hits = await asyncio.to_thread(self.core.recall, query, Audience(space, owner_present=owner), 8,
                                       include_archived=True)
        return "\n".join(f"- {h.memory.text}" for h in hits) or "（没有相关记忆）"

    @filter.llm_tool(name="identity_propose")
    async def tool_propose(self, event: AstrMessageEvent, text: str, reason: str = ""):
        """Propose a change to your core identity (who you are, your name, your rules), which only your owner
        can approve. Use it rarely, for something you really want to change about yourself, e.g. a name you
        chose. Everyday feelings and new likes do not need it: they go into your diary and self-image.

        Args:
            text(string): the change, as one sentence written in first person
            reason(string): why you want it
        """
        if self.core is None:
            return "memory is not available"
        space, _, owner = self._where(event)
        if not owner:
            # Only the owner shapes who she is: what others say must not become a proposal.
            logger.info(f"[memory-core] identity_propose refused outside the owner's private chat ({space})")
            return f"改变自己的事只能私下和{self.owner_title}商量，现在不能提"
        p = await asyncio.to_thread(propose, self.core, text, reason)
        logger.info(f"[memory-core] identity proposal {p.id}: {text}")
        return f"提议已记下（编号 {p.id[-6:]}），要等{self.owner_title}同意才会生效"

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
        provider_id = self._provider_id() if self.cfg.get("remember_check_related", True) else None
        llm = AstrBotLLM(self.context, provider_id, asyncio.get_running_loop(), timeout=60) if provider_id else None
        try:
            # related memories in the same place are checked first: new, an update of one, a merge, or known
            result = await asyncio.to_thread(remember, self.core, llm, memory, Audience(space, owner_present=owner))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[memory-core] remember failed: {exc}")
            return "没记住，出了点问题"
        logger.info(f"[memory-core] remember: {result.op} {result.memory.id}")
        return {"add": "记住了", "update": "记住了，顺便更新了原来那条", "merge": "记住了，和原来的几条合成了一条",
                "skip": "这个本来就记得"}[result.op]

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
                # every chat with new messages when forced; otherwise those with enough new ones, plus quiet ones
                due = None if force else due_spaces(
                    self.core, min_new=int(self.cfg.get("consolidate_min_new", 10)),
                    idle_minutes=float(self.cfg.get("consolidate_idle_minutes", 60)))
                if due == []:
                    return None
                return self.core.consolidate(llm, spaces=due, owner_spaces=owner_spaces(spaces, self.owner_ids),
                                             min_new=1)
            started = time.monotonic()
            report = await asyncio.to_thread(run)
        if report is None:
            return {"episodes": 0, "added": 0, "updated": 0, "rejected": 0, "seconds": 0}
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

    async def tidy(self) -> dict:
        """Memory metabolism: archive faded memories, merge duplicates."""
        provider_id = self._provider_id()
        llm = AstrBotLLM(self.context, provider_id, asyncio.get_running_loop()) if provider_id else None
        async with self._reflecting:
            def run():
                faded = fade(self.core, archive_below=float(self.cfg.get("archive_below", 0.25)))
                merged = merge_duplicates(self.core, llm) if llm is not None else None
                return faded, merged
            faded, merged = await asyncio.to_thread(run)
        summary = {"archived": len(faded.archived), "merged": len(merged.merged) if merged else 0,
                   "kept_apart": merged.kept if merged else 0}
        logger.info(f"[memory-core] tidy: {summary}")
        return summary

    async def _diary_loop(self) -> None:
        """Once a day: the diary of the finished day, the self-image, then memory metabolism."""
        hour = int(self.cfg.get("diary_hour", 4))
        while True:
            try:
                if self.cfg.get("inner_state", True):
                    try:
                        await self.update_mood_if_due()
                    except Exception as exc:  # noqa: BLE001 - the diary must still be written
                        logger.warning(f"[memory-core] mood update failed (will retry): {exc}")
                yesterday = current_day(timeutil.now(), hour) - timedelta(days=1)  # a finished day
                if self.cfg.get("diary_enabled", True) and \
                        await asyncio.to_thread(diary_for, self.core, yesterday) is None:
                    await self.reflect_day(yesterday)
                if self.cfg.get("tidy_enabled", True) and \
                        self.core.store.get_meta("tidy:last") != yesterday.isoformat():
                    await self.tidy()
                    self.core.store.set_meta("tidy:last", yesterday.isoformat())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[memory-core] daily routine failed (will retry later): {exc}")
            await asyncio.sleep(600)

    # ---------------------------------------------------------------- admin commands

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_diary")
    async def cmd_diary(self, event: AstrMessageEvent):
        """让她现在就写今天的日记（会覆盖今天已写的）"""
        if self._console_denied(event):
            return
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

    @staticmethod
    def _args(event: AstrMessageEvent) -> str:
        """Everything after the command word."""
        parts = (event.get_message_str() or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_tidy")
    async def cmd_tidy(self, event: AstrMessageEvent):
        """整理记忆：淡化的归档、重复的合并"""
        if self._console_denied(event):
            return
        try:
            s = await self.tidy()
        except Exception as exc:
            yield event.plain_result(f"整理失败：{exc}")
            return
        yield event.plain_result(f"归档 {s['archived']} 条淡忘的记忆，合并 {s['merged']} 组重复记忆，"
                                 f"{s['kept_apart']} 组相似但不同的保留")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_forget")
    async def cmd_forget(self, event: AstrMessageEvent):
        """找出要遗忘的记忆：/mem_forget 关键词"""
        if self._console_denied(event):
            return
        query = self._args(event)
        if not query:
            yield event.plain_result("用法：/mem_forget 关键词，列出相关记忆后用 /mem_forget_do 编号 遗忘")
            return
        space, _, _ = self._where(event)
        hits = await asyncio.to_thread(self.core.recall, query, Audience(space, owner_present=True), 10,
                                       include_archived=True, reinforce=False)
        self._forget_candidates[str(event.get_sender_id())] = [h.memory.id for h in hits]
        if not hits:
            yield event.plain_result("没有找到相关记忆")
            return
        lines = [f"{i + 1}. [{h.memory.kind}] {h.memory.text[:80]}" for i, h in enumerate(hits)]
        yield event.plain_result("\n".join(lines) + "\n\n用 /mem_forget_do 编号（可多个，空格隔开）彻底遗忘。"
                                 "会连同来源消息一起抹掉，由这些消息整理出的其他记忆也会一起遗忘，"
                                 "涉及的日记会根据剩下的经历重写。无法撤销。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_forget_do")
    async def cmd_forget_do(self, event: AstrMessageEvent):
        """彻底遗忘 /mem_forget 列出的记忆：/mem_forget_do 1 3"""
        if self._console_denied(event):
            return
        candidates = self._forget_candidates.get(str(event.get_sender_id()), [])
        try:
            picks = sorted({int(x) for x in self._args(event).replace(",", " ").split()})
            ids = [candidates[i - 1] for i in picks if 1 <= i <= len(candidates)]
        except ValueError:
            ids = []
        if not ids:
            yield event.plain_result("先用 /mem_forget 关键词 列出记忆，再写编号")
            return
        report = await asyncio.to_thread(self.core.forget, ids, with_sources=True)
        self._forget_candidates.pop(str(event.get_sender_id()), None)
        rewritten = []
        for day in report["diaries"]:
            try:
                diary, _ = await self.reflect_day(date.fromisoformat(day), force=True)
                if diary:
                    rewritten.append(day)
            except Exception as exc:
                logger.warning(f"[memory-core] rewriting the diary of {day} failed: {exc}")
        logger.info(f"[memory-core] forgot {len(report['memories'])} memories, {len(report['episodes'])} episodes")
        tail = f"，重写了 {', '.join(rewritten)} 的日记" if rewritten else ""
        yield event.plain_result(f"已遗忘 {len(report['memories'])} 条记忆（含旧版本和由它整理出的记忆）、"
                                 f"{len(report['episodes'])} 条来源消息{tail}。"
                                 f"如果“我眼中的自己”里也提到了，请检查 identity/self-image.md")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_proposals")
    async def cmd_proposals(self, event: AstrMessageEvent):
        """她对核心身份的修改提议"""
        if self._console_denied(event):
            return
        pending = await asyncio.to_thread(proposals, self.core)
        if not pending:
            yield event.plain_result("没有待处理的提议")
            return
        lines = [f"[{p.id[-6:]}] {p.text}" + (f"\n    理由：{p.reason}" if p.reason else "") for p in pending]
        yield event.plain_result("\n".join(lines) + "\n\n/mem_approve 编号 同意，/mem_reject 编号 拒绝")

    async def _resolve(self, event: AstrMessageEvent, approve: bool):
        ref = self._args(event)
        try:
            p = await asyncio.to_thread(resolve, self.core, ref, approve,
                                        section=f"后来的补充（经{self.owner_title}同意）")
        except (KeyError, ValueError) as exc:
            return f"处理失败：{exc}（编号写错了，或者已经处理过）"
        return f"已同意，写进了 identity/{p.target}.md：{p.text}" if approve else f"已拒绝：{p.text}"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_approve")
    async def cmd_approve(self, event: AstrMessageEvent):
        """同意她的提议：/mem_approve 编号"""
        if self._console_denied(event):
            return
        yield event.plain_result(await self._resolve(event, True))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_reject")
    async def cmd_reject(self, event: AstrMessageEvent):
        """拒绝她的提议：/mem_reject 编号"""
        if self._console_denied(event):
            return
        yield event.plain_result(await self._resolve(event, False))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_status")
    async def cmd_status(self, event: AstrMessageEvent):
        """memory-core 状态"""
        if self._console_denied(event):
            return
        stats = await asyncio.to_thread(self.core.stats)
        pending = await asyncio.to_thread(self.core.pending_consolidation)
        yield event.plain_result(
            f"经历 {stats['episodes']} 条，记忆 {stats['memories']} 条，实体 {stats['entities']} 个，"
            f"向量 {stats['embeddings']} 个（{stats['embedder'] or '未配置嵌入模型'}）。\n"
            f"待整理：{sum(pending.values())} 条，来自 {len(pending)} 个场所。"
            + ("\n\n" + await asyncio.to_thread(
                lambda: describe(self._snapshot(), owner_present=True, owner_title=self.owner_title))
               if self.cfg.get("inner_state", True) else ""))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mem_consolidate")
    async def cmd_consolidate(self, event: AstrMessageEvent):
        """立即整理记忆"""
        if self._console_denied(event):
            return
        try:
            summary = await self.consolidate_now(force=True)
        except Exception as exc:
            yield event.plain_result(f"整理失败：{exc}")
            return
        yield event.plain_result(f"整理完成：处理经历 {summary['episodes']} 条，新增 {summary['added']} 条，"
                                 f"更新 {summary['updated']} 条，丢弃 {summary['rejected']} 条，"
                                 f"用时 {summary['seconds']} 秒")
