"""Diary and self-image: how the being grows a sense of itself from what it lived.

- `write_diary` turns one day of episodes into a first-person diary entry (a `diary` memory, visible only to
  the owner, mirrored to `diary/<date>.md` for reading).
- `update_self_image` rewrites the short `identity/self-image.md` from the core identity and recent diaries.
  It is the being's own layer: it may change, but must not contradict the core identity (`identity/self.md`),
  which only the owner edits. Previous versions are kept in `identity/history/`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import timeutil
from .llm import LLM
from .models import EpisodeKind, Memory, MemoryKind
from .scope import OWNER

log = logging.getLogger("memory_core.reflect")

DIARY_PROMPT_VERSION = "diary-v1"
SELF_IMAGE_PROMPT_VERSION = "self-image-v1"
SELF_ALIAS = "self"
_DIARY_META = "diary:"
_MAX_TRANSCRIPT_CHARS = 12000

DIARY_SYSTEM = """\
{persona}

---
现在是你写日记的时间（你每天都会写一篇日记，就是现在这样写的）。根据下面“今天的经历”，用你自己的口吻写一篇今天的日记。

要求：
- 第一人称，像你平时说话那样，可以有心情、小吐槽和小动作
- 只写今天真的发生过的事和你真实的感受，不编造没发生的经历；经历里没有的事不要写成发生过
- 挑最有感觉的一两件事写，不用流水账；写写你在想什么、有什么新的发现或者想学的东西
- 150 到 400 字；不要标题、不要日期行，直接写正文
- 日记只有你和{owner}会看"""

DIARY_EMPTY_NOTE = "（今天没有人来找你说话，你也没有做别的事。）"

SELF_IMAGE_SYSTEM = """\
{persona}

---
你在整理“我眼中的自己”：一份写给自己看的、关于你现在是什么样的简短自述。它会在你以后每次说话前出现，\
帮你记得自己是谁、最近在变成什么样。

根据核心身份、旧的自述和最近的日记，重写这份自述。要求：
- 第一人称，300 字以内，用几个短条目；不要标题
- 这不是日记：不讲哪天发生了什么，不照抄日记里的句子，而是从经历里提炼出“我是个什么样的”：\
最近喜欢和在意的东西、正在学或者好奇的东西、对{owner}的感情、你注意到的自己的小习惯和变化
- 只依据日记和旧自述里有的内容，不编造；旧自述里依然成立的保留，不再成立的改掉；材料少就写短一点
- 不能和核心身份冲突，不改名字，不改底线
- 这份自述以后在别人面前也会用到：写到{owner}时只写你对他的感情和你们之间的感觉，不写他的任何具体信息和生活细节\
（吃饭、作息、身体、工作、行程、私事等）；也不写别人的隐私
- 只输出自述正文，不要解释"""


@dataclass
class DiaryResult:
    memory: Memory | None
    episodes: int
    skipped: str | None = None  # why nothing was written


def diary_key(day: date) -> str:
    return _DIARY_META + day.isoformat()


def diary_for(core, day: date) -> Memory | None:
    """The current diary entry of a day, if any."""
    memory_id = core.store.get_meta(diary_key(day)) or core.store.find_diary(day.isoformat())
    m = core.memory(memory_id) if memory_id else None
    return m if m is not None and m.status == "active" else None


def recent_diaries(core, until_day: date, days: int = 7) -> list[tuple[date, Memory]]:
    out = []
    for offset in range(days):
        day = until_day - timedelta(days=offset)
        entry = diary_for(core, day)
        if entry is not None:
            out.append((day, entry))
    return list(reversed(out))


def _transcript(core, episodes, owner_spaces: set[str], owner: str) -> str:
    me = core.entity(SELF_ALIAS)
    names: dict[str, str] = {}
    lines = []
    for ep in episodes:
        when = ep.occurred_at.astimezone().strftime("%H:%M")
        if ep.kind == EpisodeKind.MESSAGE_OUT or (me and ep.actor == me.id):
            who = "我"
        elif ep.actor:
            if ep.actor not in names:
                ent = core.store.get_entity(ep.actor)
                names[ep.actor] = ent.name if ent else "某人"
            who = names[ep.actor]
        else:
            who = "-"
        if ep.space in owner_spaces:
            where = f"和{owner}私聊"
        elif ":group:" in ep.space:
            where = "群聊"
        else:
            where = ep.space
        kind = "" if ep.kind in (EpisodeKind.MESSAGE_IN, EpisodeKind.MESSAGE_OUT) else f"（{ep.kind}）"
        lines.append(f"[{when} {where}] {who}{kind}：{ep.content}")
    text = "\n".join(lines)
    if len(text) > _MAX_TRANSCRIPT_CHARS:  # keep the end of the day; it is usually the freshest
        text = "……（前面省略）\n" + text[-_MAX_TRANSCRIPT_CHARS:]
    return text


def write_diary(core, llm: LLM, day: date, start: datetime, end: datetime, *, persona: str = "",
                owner_spaces=(), write_quiet_days: bool = True, force: bool = False,
                owner_title: str = "主人") -> DiaryResult:
    """Write the diary of `day`, from the episodes in [start, end). Idempotent unless `force`."""
    existing = diary_for(core, day)
    if existing is not None and not force:
        return DiaryResult(existing, 0, skipped="already written")
    episodes = core.episodes(since=start, until=end)
    if not episodes and not write_quiet_days:
        return DiaryResult(None, 0, skipped="nothing happened")

    owner_spaces = set(owner_spaces)
    parts = [f"日期：{day.isoformat()}（{'一二三四五六日'[day.weekday()]}）"]
    previous = recent_diaries(core, day - timedelta(days=1), days=1)
    if previous:
        parts += ["", "昨天的日记（供参考，不要重复）：", previous[-1][1].text]
    parts += ["", "今天的经历：", _transcript(core, episodes, owner_spaces, owner_title) if episodes else DIARY_EMPTY_NOTE]
    text = llm.complete(DIARY_SYSTEM.format(persona=persona.strip() or "你是一个电子生命。", owner=owner_title),
                        "\n".join(parts))
    text = text.strip()
    if not text:
        raise ValueError("the model returned an empty diary")

    me = core.ensure_entity(SELF_ALIAS)
    memory = Memory(
        kind=MemoryKind.DIARY, text=text, scope=OWNER, subjects=[me.id], importance=0.6,
        sources=[ep.id for ep in episodes], derived_by=f"{llm.model_id}/{DIARY_PROMPT_VERSION}",
        valid_from=start, meta={"date": day.isoformat()},
    )
    saved = core.supersede(existing.id, memory) if existing is not None else core.add_memory(memory)
    core.store.set_meta(diary_key(day), saved.id)
    _mirror(core, day, text)
    return DiaryResult(saved, len(episodes))


def _mirror(core, day: date, text: str) -> None:
    """A plain file per day, for people to read. Derived data: the memory is the source of truth."""
    folder = core.root / "diary"
    folder.mkdir(exist_ok=True)
    (folder / f"{day.isoformat()}.md").write_text(f"# {day.isoformat()}\n\n{text}\n", encoding="utf-8",
                                                  newline="\n")


def update_self_image(core, llm: LLM, today: date, *, persona: str = "", days: int = 7,
                      owner_title: str = "主人") -> str | None:
    """Rewrite identity/self-image.md from the core identity and the last `days` diaries."""
    diaries = recent_diaries(core, today, days)
    if not diaries:
        return None
    old = core.identity("self-image") or ""
    user = "\n".join([
        "旧的自述：", old.strip() or "（还没有）", "",
        f"最近 {len(diaries)} 天的日记：",
        *(f"【{d.isoformat()}】\n{m.text}\n" for d, m in diaries),
    ])
    system = SELF_IMAGE_SYSTEM.format(persona=persona.strip() or "你是一个电子生命。", owner=owner_title)
    text = llm.complete(system, user).strip()
    if not text:
        raise ValueError("the model returned an empty self-image")
    if old.strip():
        history = core.root / "identity" / "history"
        history.mkdir(exist_ok=True)
        stamp = timeutil.now().astimezone().strftime("%Y%m%d-%H%M%S")
        (history / f"self-image-{stamp}.md").write_text(old, encoding="utf-8", newline="\n")
    core.set_identity("self-image", text + "\n")
    return text


def day_window(day: date, boundary_hour: int) -> tuple[datetime, datetime]:
    """A 'day' runs from boundary_hour local time to the same hour next day (late nights belong to the day)."""
    start = datetime(day.year, day.month, day.day, boundary_hour).astimezone()
    return start, start + timedelta(days=1)


def current_day(now: datetime, boundary_hour: int) -> date:
    """The 'day' that `now` (aware) belongs to."""
    local = now.astimezone()
    return (local - timedelta(hours=boundary_hour)).date()
