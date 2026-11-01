"""Framework-independent helpers of the AstrBot adapter (unit-tested without AstrBot)."""

from __future__ import annotations

import re
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


def owner_console(platform: str | None, is_group: bool, sender_id: str, owner_ids: Iterable[str],
                  platforms: Iterable[str]) -> bool:
    """Whether a message may run the owner's memory commands (forget, approve, ...): the owner, in a
    private chat, on a platform listed for it (by default only AstrBot's local webchat)."""
    allowed = {str(p).strip() for p in platforms if str(p).strip()}
    return is_owner_private(is_group, sender_id, owner_ids) and (platform or "") in allowed


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
CHANGING_DOCS = ("self-image",)
"""Rewritten by the being every day: they go after what stays the same in the system prompt."""


def memory_guide(owner_title: str = "主人", diary_hour: int = 4, *, diary: bool = True,
                 tidy: bool = True, diary_now: bool = False) -> str:
    """How the being's memory works, so it neither denies having one nor asks for it to be "turned on".
    The daily schedule follows the plugin settings, so what it says about itself is true."""
    daily = []
    if diary:
        daily.append(
            f"- 每天凌晨{diary_hour}点左右，你会回顾前一天写一篇日记（凌晨{diary_hour}点前熬的夜算前一天；"
            f"日记只有{owner_title}能看），再根据最近一周的日记更新 <self_image>，也就是“我眼中的自己”\n")
        if diary_now:
            daily.append("- 白天想写也可以自己写（diary_write）：写过的会接着往下写，凌晨再把之后的事补上\n")
    if tidy:
        when = "写完日记后" if diary else f"每天凌晨{diary_hour}点左右"
        daily.append(
            f"- {when}还会整理一次记忆：很久没想起、又不太重要的事淡忘收起来，说的是同一件事的几条记忆合并成一条\n")
    return (
        "<how_your_memory_works>\n"
        "- 你有长期记忆，一直在工作，不需要谁去打开：你看到的对话会自动记下来，每隔一段时间整理成长期记忆\n"
        "- 和当前话题有关的记忆会自动出现在 <long_term_memory> 里\n"
        "- 对方消息后面附着的 <now>、<long_term_memory> 这类标签是系统给你的提示，只对这一句有效，不是对方说的话\n"
        "- 想不起来、或者想确认某件事时，用 memory_recall 主动翻找记忆；很久没想起的事会慢慢淡忘，"
        "不会自己冒出来，但主动翻找还能找回来\n"
        "- 特别重要、想马上记牢的事（比如有人让你记住），用 memory_remember 记下来；普通聊天不用逐条记\n"
        + "".join(daily) +
        "- 上面的身份文档就是你自己，和记忆一起被保存着。核心身份只有"
        f"{owner_title}能改；你真的想改自己的某一点时（比如给自己取了名字），用 identity_propose 提议，"
        f"等{owner_title}同意\n"
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


TRANSIENT_TAGS = ("long_term_memory", "now", "abilities_news")
"""Blocks added to the user's message for this reply only: taken out of the earlier messages in the history."""


def strip_transient(contexts: list, tags: Iterable[str] = TRANSIENT_TAGS) -> int:
    """Remove `<tag>...</tag>` blocks from the earlier user messages in the request history.

    They were added for one reply and are stored with it; leaving them would make every later request carry
    old memories and an old "now". Taking them out the same way each time keeps the history identical between
    requests, so the model provider's prompt cache covers it. Works on OpenAI-style dicts or objects with
    role/content; a message is never left empty. Returns how many blocks were removed."""
    pattern = re.compile(r"\s*<(" + "|".join(re.escape(t) for t in tags) + r")>.*?</\1>\s*", re.S)

    def get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)

    def put(obj, key, value):
        if isinstance(obj, dict):
            obj[key] = value
        else:
            setattr(obj, key, value)

    removed = 0
    for msg in contexts or []:
        if get(msg, "role") != "user":
            continue
        content = get(msg, "content")
        if isinstance(content, str):
            new, n = pattern.subn("\n\n", content)
            if n and new.strip():
                put(msg, "content", new.strip())
                removed += n
        elif isinstance(content, list):
            keep, edits, n_all = [], [], 0
            for part in content:
                text = get(part, "text")
                if isinstance(text, str) and pattern.search(text):
                    new, n = pattern.subn("\n\n", text)
                    n_all += n
                    if new.strip():
                        edits.append((part, new.strip()))
                        keep.append(part)
                    continue
                keep.append(part)
            if n_all and keep:  # never leave a message empty
                for part, new in edits:
                    put(part, "text", new)
                content[:] = keep
                removed += n_all
    return removed
