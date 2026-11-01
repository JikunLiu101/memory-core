"""What the being can do, and noticing when that changes.

The host collects the being's abilities from whatever is installed (each a stable key, a short name that reads as
a verb phrase, e.g. "看图", and one line on how it works). The list goes into the prompt, so the being knows what it
can and cannot do. The last known list is kept in the store: an ability that appears is marked new for a while and
the being notices it ("I can do this now"), one that disappears is noticed too, so it stops promising it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from . import timeutil

META_KEY = "abilities"


@dataclass(frozen=True)
class Ability:
    key: str  # stable, e.g. "memory-core:vision"
    name: str  # a verb phrase: "看图", "上网逛逛"
    text: str  # how it works, one line


@dataclass
class Noticed:
    gained: list[Ability] = field(default_factory=list)  # just appeared (this call)
    lost: list[Ability] = field(default_factory=list)  # just disappeared (this call)
    fresh: set[str] = field(default_factory=set)  # keys gained within the fresh window
    gone: list[Ability] = field(default_factory=list)  # lost within the fresh window
    first: bool = False  # nothing was known before: this list is the baseline, nothing is "new"


def _load(core) -> dict | None:
    raw = core.store.get_meta(META_KEY)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def notice(core, current: Iterable[Ability], *, now: datetime | None = None, fresh_hours: float = 24,
           update: bool = True) -> Noticed:
    """Compare `current` with the last known abilities. With `update`, the change is remembered (so it is
    noticed once); without, nothing is written (e.g. outside the owner's private chat)."""
    now = now or timeutil.now()
    current = {a.key: a for a in current}
    state = _load(core)
    out = Noticed(first=state is None)
    state = state or {"known": {}, "lost": []}
    known: dict = state.get("known") or {}
    lost: list = state.get("lost") or []
    window = timedelta(hours=fresh_hours)
    changed = out.first
    if not out.first:
        out.gained = [a for k, a in current.items() if k not in known]
        out.lost = [Ability(k, v.get("name", k), v.get("text", "")) for k, v in known.items() if k not in current]
    for a in out.gained:
        known[a.key] = {"name": a.name, "text": a.text, "since": now.isoformat()}
        lost = [x for x in lost if x.get("key") != a.key]
    for a in out.lost:
        known.pop(a.key, None)
        lost.append({"key": a.key, "name": a.name, "text": a.text, "at": now.isoformat()})
    for k, a in current.items():
        if k not in known:  # the baseline
            known[k] = {"name": a.name, "text": a.text, "since": ""}
        elif (known[k].get("name"), known[k].get("text")) != (a.name, a.text):
            known[k].update(name=a.name, text=a.text)  # reworded: not new
            changed = True
    changed = changed or bool(out.gained or out.lost)

    def recent(at: str) -> bool:
        try:
            return bool(at) and now - datetime.fromisoformat(at) < window
        except ValueError:
            return False
    out.fresh = {k for k, v in known.items() if k in current and recent(v.get("since", ""))}
    kept = [x for x in lost if recent(x.get("at", ""))]
    changed = changed or len(kept) != len(lost)
    out.gone = [Ability(x["key"], x.get("name", x["key"]), x.get("text", "")) for x in kept]
    if update and changed:
        core.store.set_meta(META_KEY, json.dumps({"known": known, "lost": kept}, ensure_ascii=False))
    return out


def gained_note(a: Ability) -> str:
    """The being's own note when it finds a new ability (recorded as a thought)."""
    return f"我发现自己多了一项本事：{a.name}"


def lost_note(a: Ability) -> str:
    return f"我发现自己不能再{a.name}了"


def abilities_block(current: Iterable[Ability], noticed: Noticed | None = None, owner: str = "主人", *,
                    marks: bool = True) -> str:
    """The list for the prompt. Empty when there is nothing to list.

    With `marks`, what is new or just lost is marked in the list. Without, the list only changes when an ability
    comes or goes (it can stay in a cached system prompt), and `abilities_news` says what is new."""
    current = list(current)
    if not current:
        return ""
    fresh = noticed.fresh if (noticed and marks) else set()
    lines = ["<what_you_can_do>",
             "这是按你现在真正装着、开着的本事列的，会随着装上或拿掉而变："]
    for a in current:
        lines.append(f"- {a.name}{'（刚有的，你才发现）' if a.key in fresh else ''}：{a.text}")
    for a in (noticed.gone if (noticed and marks) else []):
        lines.append(f"- 刚发现不能再{a.name}了：别再答应这件事")
    lines.append(f"- 上面没列的，就是你现在还不会的。做不到的事别答应，也别说成做过；可以老实说还不会，"
                 f"想学就告诉{owner}")
    lines.append("- 说要做清单上的事（写日记、补一句、记住、上网……），就在这次回复里当场用对应的工具去做；"
                 "没用工具就别说已经做了或马上去做")
    if fresh:
        lines.append(f"- 刚有的本事，想说就自然地跟{owner}提一句、试一试，不用每次都说")
    lines.append("</what_you_can_do>")
    return "\n".join(lines)


NEWS_TAG = "abilities_news"


def abilities_news(current: Iterable[Ability], noticed: Noticed | None, owner: str = "主人") -> str:
    """What is new or just lost, for the message of this reply (the list itself stays in the system prompt).
    Empty when nothing changed within the fresh window."""
    if not noticed:
        return ""
    names = {a.key: a.name for a in current}
    new = [names[k] for k in names if k in noticed.fresh]
    lines = []
    if new:
        lines.append(f"- 刚有的本事（你才发现）：{'、'.join(new)}。想说就自然地跟{owner}提一句、试一试，不用每次都说")
    for a in noticed.gone:
        lines.append(f"- 刚发现不能再{a.name}了：别再答应这件事")
    if not lines:
        return ""
    return f"<{NEWS_TAG}>\n" + "\n".join(lines) + f"\n</{NEWS_TAG}>"
