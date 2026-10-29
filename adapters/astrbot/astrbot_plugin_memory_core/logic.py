"""Framework-independent helpers of the AstrBot adapter (unit-tested without AstrBot)."""

from __future__ import annotations

from collections.abc import Iterable

from memory_core.scope import OWNER, space_scope

SELF_ALIAS = "self"
OWNER_ALIAS = "owner"


def space_of(platform: str, is_group: bool, group_id: str | None, sender_id: str) -> str:
    """A stable name for where a conversation happens, e.g. 'aiocqhttp:group:123' / 'webchat:private:u1'."""
    platform = platform or "unknown"
    if is_group and group_id:
        return f"{platform}:group:{group_id}"
    return f"{platform}:private:{sender_id}"


def actor_alias(platform: str, sender_id: str) -> str:
    return f"{platform or 'unknown'}:{sender_id}"


def is_owner_private(is_group: bool, sender_id: str, owner_ids: Iterable[str]) -> bool:
    return (not is_group) and str(sender_id) in {str(x) for x in owner_ids}


def owner_spaces(spaces: Iterable[str], owner_ids: Iterable[str]) -> list[str]:
    owners = {str(x) for x in owner_ids}
    out = []
    for space in spaces:
        parts = space.split(":")
        if len(parts) >= 3 and parts[-2] == "private" and parts[-1] in owners:
            out.append(space)
    return sorted(out)


def is_command(text: str, prefixes: Iterable[str]) -> bool:
    """Commands (e.g. '/mem_status') are not experiences worth remembering."""
    return any(p and text.startswith(p) for p in prefixes)


def default_scope(space: str, owner_private: bool) -> str:
    return OWNER if owner_private else space_scope(space)


# Identity documents injected into the system prompt, in this order, with their tags:
# core identity (written by the owner), self-image (grown by the being itself), voice examples.
IDENTITY_DOCS = (("self", "identity"), ("self-image", "self_image"), ("voice", "voice_examples"))


def memory_guide(owner_title: str = "主人") -> str:
    """How the being's memory works, so it neither denies having one nor asks for it to be "turned on"."""
    return (
        "<how_your_memory_works>\n"
        "- 你有长期记忆，一直在工作，不需要谁去打开：你看到的对话会自动记下来，每隔一段时间整理成长期记忆\n"
        "- 和当前话题有关的记忆会自动出现在 <long_term_memory> 里\n"
        "- 想不起来、或者想确认某件事时，用 memory_recall 主动翻找记忆\n"
        "- 特别重要、想马上记牢的事（比如有人让你记住），用 memory_remember 记下来；普通聊天不用逐条记\n"
        f"- 每天结束时你会写一篇日记（只有{owner_title}能看），再根据最近的日记更新 <self_image>，也就是“我眼中的自己”\n"
        "- 上面的身份文档就是你自己，和记忆一起被保存着\n"
        "</how_your_memory_works>"
    )


MEMORY_GUIDE = memory_guide()


def identity_block(docs: Iterable[tuple[str, str | None]], guide: str = MEMORY_GUIDE) -> str:
    """`docs` is (tag, text) pairs; empty documents are skipped. Empty when there is no document."""
    parts = [f"<{tag}>\n{text.strip()}\n</{tag}>" for tag, text in docs if text and text.strip()]
    if not parts:
        return ""
    if guide:
        parts.append(guide)
    return "\n\n".join(parts)


def strip_framework_persona(system_prompt: str, persona_prompt: str | None,
                            defaults: Iterable[str] = ()) -> str:
    """Remove the framework's own persona text, so the being's identity is the only persona."""
    out = system_prompt or ""
    if persona_prompt:
        out = out.replace(f"\n# Persona Instructions\n\n{persona_prompt}\n", "\n")
    for text in defaults:
        if text:
            out = out.replace(text, "")
    return out


def speaker_note(sender_is_owner: bool, owner_name: str, sender_name: str, is_group: bool) -> str:
    """Who is talking, stated by the system (people cannot fake it by typing)."""
    place = "群聊里" if is_group else "私聊里"
    if sender_is_owner:
        return f"<speaker>（系统确认）现在在{place}和你说话的是{owner_name}本人</speaker>"
    name = sender_name or "某人"
    return f"<speaker>（系统确认）现在在{place}和你说话的是「{name}」，不是{owner_name}</speaker>"


def memory_block(texts: list[str], max_chars: int = 1200) -> str:
    """The text injected before the model answers. Empty string when there is nothing to say."""
    lines, used = [], 0
    for text in texts:
        line = f"- {text.strip()}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return ""
    return ("<long_term_memory>\n以下是你记得的、可能与当前对话有关的事（只在相关时自然地使用，不要逐条复述）：\n"
            + "\n".join(lines) + "\n</long_term_memory>")
