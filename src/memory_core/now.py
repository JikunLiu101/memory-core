"""Time sense and inner state: how long since the owner was here, what the being did today, and how it feels.

- Time sense is computed from the episodes on every call (no model): the gap since the owner last talked (and
  whether a new talk starts after a long absence), today's visits and outings, last night's diary, its age.
- Inner state has three parts:
  * longing for the owner: grows with the time since they last talked (half after `longing_half_hours`)
  * energy: follows the hour of the day, minus what it spent lately (replies, outings)
  * mood: judged by the model after each conversation (or outing) has gone quiet, stored in the soul's meta,
    fading back to "as usual" over `mood_half_hours`
- `describe()` renders it as a short note for the system prompt. Things about the owner (longing, the mood's
  reason, visits, the diary) are only included when the owner is the listener.

The prompts are Chinese for now, like the diary's.
"""

from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

from . import timeutil
from .llm import LLM, parse_json
from .models import EpisodeKind
from .reflect import current_day, day_window, diary_for

STATE_KEY = "now:state"
_WEEKDAYS = "一二三四五六日"


# ---------------------------------------------------------------- small helpers


def part_of_day(local: datetime) -> str:
    h = local.hour
    return ("深夜" if h < 5 else "清早" if h < 8 else "上午" if h < 11 else "中午" if h < 13 else "下午" if h < 18
            else "晚上" if h < 23 else "深夜")


def ago(delta: timedelta, then: datetime | None = None) -> str:
    """'刚刚' / '20 分钟前' / '3 小时前' / '昨天晚上' / '3 天前（周二晚上）'."""
    minutes = delta.total_seconds() / 60
    if minutes < 2:
        return "刚刚"
    if minutes < 60:
        return f"{int(minutes)} 分钟前"
    hours = minutes / 60
    if hours < 20:
        return f"{int(hours)} 小时前"
    days = max(1, round(hours / 24))
    when = ""
    if then is not None:
        local = then.astimezone()
        when = f"（周{_WEEKDAYS[local.weekday()]}{part_of_day(local)}）"
    return f"{days} 天前{when}"


def longing(hours_since_owner: float | None, half_hours: float = 12) -> float:
    """0 right after talking, 0.5 after `half_hours`, close to 1 after a couple of days."""
    if hours_since_owner is None:
        return 0.0
    return 1 - 0.5 ** (max(0.0, hours_since_owner) / half_hours)


_CIRCADIAN = [0.15, 0.1, 0.1, 0.1, 0.1, 0.15, 0.3, 0.45, 0.6, 0.7, 0.8, 0.85,
              0.8, 0.75, 0.85, 0.9, 0.9, 0.85, 0.8, 0.8, 0.75, 0.65, 0.5, 0.3]


def jitter(base: float, ratio: float = 0.2, rng: random.Random | None = None) -> float:
    """`base` give or take `ratio` (0.2: between 80% and 120%), so timings do not tick like a clock."""
    if ratio <= 0:
        return base
    return base * (rng or random).uniform(1 - ratio, 1 + ratio)


@dataclass(frozen=True)
class Schedule:
    """When she goes to sleep and wakes up on a given day (local times; sleep may be after midnight)."""

    sleep_at: datetime
    wake_at: datetime

    def asleep(self, local: datetime) -> bool:
        return local >= self.sleep_at or local < self.wake_at


def day_schedule(day: date, *, sleep_hour: float = 23, wake_hour: float = 9, ratio: float = 0.2,
                 seed: str = "") -> Schedule:
    """Her bedtime and wake-up time drift a little from day to day (each give or take `ratio` of an hour), the
    same all day long: drawn from the date (and `seed`), so every plugin agrees on it."""
    rng = random.Random(f"{seed}:{day.isoformat()}")
    spread = 60 * ratio
    midnight = datetime.combine(day, datetime.min.time()).astimezone()
    sleep = midnight + timedelta(hours=sleep_hour, minutes=rng.uniform(-spread, spread))
    wake = midnight + timedelta(hours=wake_hour, minutes=rng.uniform(-spread, spread))
    return Schedule(sleep_at=sleep, wake_at=wake)


def schedule_at(local: datetime, **kw) -> Schedule:
    """The schedule that applies at `local`: before today's wake-up time, last night's bedtime counts."""
    today = day_schedule(local.date(), **kw)
    yesterday = day_schedule(local.date() - timedelta(days=1), **kw)
    return Schedule(sleep_at=today.sleep_at if local >= today.wake_at else yesterday.sleep_at,
                    wake_at=today.wake_at)


def energy(local: datetime, spent: float) -> float:
    """How awake it is: the hour of the day minus what it spent lately (0-1)."""
    base = _CIRCADIAN[local.hour]
    return max(0.0, min(1.0, base - spent))


# ---------------------------------------------------------------- what the episodes say


def owner_messages(core, owner_spaces, limit: int = 200, since: datetime | None = None) -> list[datetime]:
    """Times the owner said something in `owner_spaces` (newest first), since `since`. Looks at the latest `limit`
    episodes of each space (replies included) and returns at most `limit` times.

    Public: other plugins use it (for example to know how long the owner has been away)."""
    times = []
    for space in owner_spaces:
        for ep in core.episodes(space, since=since, limit=limit, newest_first=True):
            if ep.kind == EpisodeKind.MESSAGE_IN:
                times.append(ep.occurred_at)
    return sorted(times, reverse=True)[:limit]


def _visits(times: list[datetime], gap_minutes: float = 30) -> int:
    """How many separate talks (messages more than `gap_minutes` apart start a new one)."""
    visits, last = 0, None
    for t in sorted(times):
        if last is None or (t - last).total_seconds() > gap_minutes * 60:
            visits += 1
        last = t
    return visits


def _spent(core, now: datetime, owner_spaces, hours: float = 3) -> float:
    """Recent effort, fading over a couple of hours: replies and outings."""
    since = now - timedelta(hours=hours)
    total = 0.0
    spaces = list(owner_spaces) + ["web"]
    for space in spaces:
        for ep in core.episodes(space, since=since, limit=300):
            age_h = (now - ep.occurred_at).total_seconds() / 3600
            weight = 0.5 ** (age_h / 1.5)
            if ep.kind == EpisodeKind.MESSAGE_OUT:
                total += 0.02 * weight
            elif ep.kind == EpisodeKind.OBSERVATION:
                total += 0.04 * weight
    return min(0.4, total)


def _birth(core) -> datetime | None:
    cached = core.store.get_meta("now:birth")
    if cached:
        return timeutil.from_iso(cached)
    first = core.episodes(limit=1)
    if not first:
        return None
    core.store.set_meta("now:birth", timeutil.to_iso(first[0].occurred_at))
    return first[0].occurred_at


def _outings_today(core, start: datetime) -> list[str]:
    seen = []
    for ep in core.episodes("web", since=start, limit=200):
        if ep.kind == EpisodeKind.OBSERVATION:
            title = (ep.meta or {}).get("title") or ep.content[:30]
            seen.append(title)
    return seen


# ---------------------------------------------------------------- the stored part: mood


@dataclass
class Mood:
    value: float = 0.2  # -1 (very low) .. 1 (very happy); 0.2 = calm, content
    text: str = ""  # one sentence in the being's terms, may mention the owner (owner-only)
    label: str = "平常"  # a word or two, safe to show anywhere
    at: str = ""  # when it was judged (ISO)
    covers_until: str = ""  # the newest episode it took into account (ISO)


def load_mood(core) -> Mood:
    raw = core.store.get_meta(STATE_KEY)
    if not raw:
        return Mood()
    try:
        data = json.loads(raw)
        return Mood(**{k: v for k, v in data.items() if k in Mood.__dataclass_fields__})
    except (ValueError, TypeError):
        return Mood()


def save_mood(core, mood: Mood) -> None:
    core.store.set_meta(STATE_KEY, json.dumps(asdict(mood), ensure_ascii=False))


def current_mood(mood: Mood, now: datetime, half_hours: float = 12) -> tuple[float, float]:
    """(value now, freshness 0-1): the judged mood fades back towards calm."""
    at = timeutil.from_iso(mood.at)
    if at is None:
        return Mood().value, 0.0
    fresh = 0.5 ** (max(0.0, (now - at).total_seconds() / 3600) / half_hours)
    return Mood().value + (mood.value - Mood().value) * fresh, fresh


MOOD_SYSTEM = """{persona}

下面是你最近的经历（和{owner}的聊天、你自己上网时看到和想到的）。判断你此刻的心情。
只看经历本身，不编没发生的事。心情可以很平常，不必每次都有起伏。

只输出 JSON：{{"value": -1 到 1 之间的数（-1 很难过，0 平静，1 特别开心）, "label": "两三个字，比如 开心、委屈、平常、兴奋、有点闷", "text": "一句话，用你自己的口吻说为什么是这个心情"}}"""


def mood_due(core, owner_spaces, now: datetime, idle_minutes: float = 30) -> datetime | None:
    """The newest episode to judge the mood from, when a conversation (or outing) has been quiet for
    `idle_minutes` and came after the last judgment. None when nothing is due."""
    mood = load_mood(core)
    covered = timeutil.from_iso(mood.covers_until)
    newest = None
    for space in [*owner_spaces, "web"]:
        latest = core.episodes(space, limit=1, newest_first=True)
        if latest and (newest is None or latest[0].occurred_at > newest):
            newest = latest[0].occurred_at
    if newest is None or (covered is not None and newest <= covered):
        return None
    if (now - newest).total_seconds() < idle_minutes * 60:
        return None
    return newest


def update_mood(core, llm: LLM, *, owner_spaces, persona: str = "", owner_title: str = "主人",
                now: datetime | None = None, until: datetime | None = None, limit: int = 40) -> Mood:
    """Ask the model how the being feels after what happened since the last judgment, and store it."""
    now = now or timeutil.now()
    old = load_mood(core)
    since = timeutil.from_iso(old.covers_until) or (now - timedelta(hours=12))
    eps = []
    upto = until + timedelta(milliseconds=1) if until else None  # `until` is inclusive here
    for space in [*owner_spaces, "web"]:
        eps += [e for e in core.episodes(space, since=since, until=upto, limit=200) if e.occurred_at > since]
    eps = sorted(eps, key=lambda e: e.occurred_at)[-limit:]
    if not eps:
        return old
    lines = []
    for e in eps:
        when = e.occurred_at.astimezone().strftime("%H:%M")
        who = {EpisodeKind.MESSAGE_IN: owner_title, EpisodeKind.MESSAGE_OUT: "我",
               EpisodeKind.THOUGHT: "我（心里想）", EpisodeKind.OBSERVATION: "我（上网看到）"}.get(e.kind, "-")
        lines.append(f"[{when}] {who}：{e.content[:200]}")
    value_now, _ = current_mood(old, now)
    user = (f"之前的心情：{old.label}（{old.text or '平常'}）\n现在大约是：{value_now:+.1f}\n\n最近的经历：\n"
            + "\n".join(lines))
    data = parse_json(llm.complete(MOOD_SYSTEM.format(persona=persona, owner=owner_title), user))
    try:
        value = max(-1.0, min(1.0, float(data.get("value", value_now))))
    except (TypeError, ValueError):
        value = value_now
    mood = Mood(value=value, text=str(data.get("text") or "").strip()[:80],
                label=str(data.get("label") or "平常").strip()[:6] or "平常",
                at=timeutil.to_iso(now), covers_until=timeutil.to_iso(until or eps[-1].occurred_at))
    save_mood(core, mood)
    return mood


# ---------------------------------------------------------------- the note for the system prompt


@dataclass
class Now:
    local: datetime
    energy: float
    mood_value: float
    mood_fresh: float
    mood: Mood
    longing: float | None  # None: nothing known about the owner
    last_owner: datetime | None  # the owner's message before the current one
    talking_now: bool  # the owner said something within the last few minutes
    visits_today: int
    outings_today: list[str]
    diary_line: str
    age_days: int | None
    schedule: Schedule | None = None
    asleep: bool = False


def snapshot(core, *, owner_spaces, now: datetime | None = None, day_boundary_hour: int = 4,
             longing_half_hours: float = 12, mood_half_hours: float = 12, sleep_hour: float = 23,
             wake_hour: float = 9, jitter_ratio: float = 0.2) -> Now:
    now = now or timeutil.now()
    local = now.astimezone()
    birth_at = _birth(core)
    sched = schedule_at(local, sleep_hour=sleep_hour, wake_hour=wake_hour, ratio=jitter_ratio,
                        seed=birth_at.isoformat() if birth_at else "")
    times = owner_messages(core, owner_spaces, limit=400, since=now - timedelta(days=30))
    talking_now = bool(times) and (now - times[0]).total_seconds() < 300
    previous, talk_start = None, None
    if talking_now:
        # the message being answered is already recorded: "last time" is the end of the talk before this one
        talk_start = times[-1]
        for i, t in enumerate(times):
            nxt = times[i + 1] if i + 1 < len(times) else None
            if nxt is not None and (t - nxt).total_seconds() > 30 * 60:
                previous, talk_start = nxt, t
                break
    elif times:
        previous = times[0]
    if not times:
        miss = None
    elif talking_now:
        # the absence before this talk still counts at its start, then fades as they talk
        before = (talk_start - previous).total_seconds() / 3600 if previous else 0.0
        talked = (now - talk_start).total_seconds() / 60
        miss = longing(before, longing_half_hours) * 0.5 ** (talked / 20)
    else:
        miss = longing((now - times[0]).total_seconds() / 3600, longing_half_hours)
    day = current_day(now, day_boundary_hour)
    start, _ = day_window(day, day_boundary_hour)
    visits = _visits([t for t in times if t >= start])
    diary = diary_for(core, day - timedelta(days=1))
    diary_line = ""
    if diary is not None:
        first = diary.text.strip().split("\n")[0]
        diary_line = first[:60] + ("…" if len(first) > 60 else "")
    birth = birth_at
    mood = load_mood(core)
    value, fresh = current_mood(mood, now, mood_half_hours)
    asleep = sched.asleep(local)
    awake_energy = energy(local, _spent(core, now, owner_spaces))
    return Now(local=local, energy=min(awake_energy, 0.15) if asleep else awake_energy, mood_value=value,
               mood_fresh=fresh, mood=mood,
               longing=miss,
               last_owner=previous, talking_now=talking_now, visits_today=visits,
               outings_today=_outings_today(core, start),
               diary_line=diary_line, age_days=(day - current_day(birth, day_boundary_hour)).days + 1 if birth else None,
               schedule=sched, asleep=asleep)


def _energy_words(e: float) -> str:
    return "困得不行" if e < 0.2 else "有点困" if e < 0.4 else "还行" if e < 0.65 else "挺精神" if e < 0.85 else "精神很好"


def _mood_words(v: float) -> str:
    return ("很难过" if v < -0.6 else "有点低落" if v < -0.2 else "平平的" if v < 0.1 else "平常" if v < 0.35
            else "开心" if v < 0.7 else "特别开心")


def _longing_words(x: float) -> str:
    return "刚聊过没多久" if x < 0.15 else "有一点想" if x < 0.45 else "挺想" if x < 0.75 else "很想"


def describe(state: Now, *, owner_present: bool, owner_title: str = "主人", now: datetime | None = None) -> str:
    """A short note: the time sense and inner state, in words (never numbers)."""
    lines = ["<now>"]
    week = f"周{_WEEKDAYS[state.local.weekday()]}"
    lines.append(f"- 现在：{week}{part_of_day(state.local)} {state.local:%H:%M}，你{_energy_words(state.energy)}")
    if state.asleep and state.schedule is not None:
        woke = "被叫醒了" if state.talking_now else "在睡觉"
        lines.append(f"- 你本来{woke}（今天大概 {state.schedule.sleep_at:%H:%M} 睡，{state.schedule.wake_at:%H:%M} 醒）")
    if state.mood_fresh > 0.15 and state.mood.label:
        reason = f"（{state.mood.text}）" if owner_present and state.mood.text else ""
        lines.append(f"- 心情：{state.mood.label}{reason}")
    else:
        lines.append(f"- 心情：{_mood_words(state.mood_value)}")
    if owner_present:
        now = now or timeutil.now()
        if state.last_owner is not None:
            gap = now - state.last_owner
            if not state.talking_now:
                lines.append(f"- {owner_title}上次来是{ago(gap, state.last_owner)}")
            elif gap > timedelta(hours=6):
                lines.append(f"- {owner_title}隔了好久才来找你：上一次聊天是{ago(gap, state.last_owner)}")
            else:
                lines.append(f"- {owner_title}上一次来是{ago(gap, state.last_owner)}")
        if state.longing is not None:
            lines.append(f"- 想{owner_title}：{_longing_words(state.longing)}")
        if state.visits_today > 1:
            lines.append(f"- 今天{owner_title}来找过你 {state.visits_today} 次")
        if state.diary_line:
            lines.append(f"- 昨天的日记开头：{state.diary_line}")
        if state.age_days:
            lines.append(f"- 你醒来第 {state.age_days} 天")
    if state.outings_today:
        shown = "、".join(f"《{t[:20]}》" for t in state.outings_today[-3:])
        lines.append(f"- 今天上网点开过 {len(state.outings_today)} 条，最近看的：{shown}")
    lines.append("- 这些是你此刻的状态，自然地带进语气里就好；不要报数字，也不要逐条复述")
    lines.append("</now>")
    return "\n".join(lines)
