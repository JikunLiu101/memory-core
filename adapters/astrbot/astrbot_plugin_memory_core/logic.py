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
