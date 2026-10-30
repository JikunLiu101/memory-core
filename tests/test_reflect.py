from datetime import date, datetime, timedelta, timezone

from memory_core import Audience, Episode
from memory_core.reflect import (
    current_day,
    day_window,
    diary_for,
    recent_diaries,
    update_self_image,
    write_diary,
)

DM = "webchat:private:dad"
G1 = "qq:group:g1"


class FakeLLM:
    model_id = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.answers.pop(0)


def _window():
    start = datetime(2026, 10, 29, 4, tzinfo=timezone.utc)
    return date(2026, 10, 29), start, start + timedelta(days=1)


def _say(core, space, actor, text, when):
    kind = "message_out" if actor == "self" else "message_in"
    return core.record(Episode(kind=kind, content=text, space=space, source="t", actor=actor, occurred_at=when))


def test_diary_from_one_day_is_owner_only_and_idempotent(core):
    day, start, end = _window()
    core.ensure_entity("owner", name="主人")
    _say(core, DM, "owner", "今天吃了鸡胸肉", start + timedelta(hours=10))
    _say(core, DM, "self", "就这点吗", start + timedelta(hours=10, minutes=1))
    _say(core, DM, "owner", "昨天的事", start - timedelta(hours=1))  # previous day: excluded
    llm = FakeLLM("今天主人只吃了鸡胸肉，我有点担心")
    result = write_diary(core, llm, day, start, end, persona="我是小星", owner_spaces=[DM],
                         owner_title="老板")
    assert result.episodes == 2 and result.memory.kind == "diary" and result.memory.scope == "owner"
    system, user = llm.calls[0]
    assert system.startswith("我是小星") and "日记只有你和老板会看" in system
    assert "和老板私聊] 主人：今天吃了鸡胸肉" in user and "昨天的事" not in user
    assert "我：就这点吗" in user
    assert (core.root / "diary" / "2026-10-29.md").read_text(encoding="utf-8").endswith("我有点担心\n")
    # second call: nothing new written
    again = write_diary(core, FakeLLM(), day, start, end)
    assert again.skipped == "already written" and again.memory.id == result.memory.id
    # visible to the owner only
    assert core.recall("鸡胸肉", Audience(DM, owner_present=True), reinforce=False)
    assert not [h for h in core.recall("鸡胸肉", Audience(G1), reinforce=False) if h.memory.kind == "diary"]


def test_forced_rewrite_supersedes(core):
    day, start, end = _window()
    _say(core, DM, "self", "hi", start + timedelta(hours=1))
    first = write_diary(core, FakeLLM("v1"), day, start, end).memory
    second = write_diary(core, FakeLLM("v2"), day, start, end, force=True).memory
    assert diary_for(core, day).id == second.id and core.memory(first.id).status == "superseded"


def test_quiet_day(core):
    day, start, end = _window()
    assert write_diary(core, FakeLLM(), day, start, end, write_quiet_days=False).skipped == "nothing happened"
    llm = FakeLLM("今天没人来找我")
    assert write_diary(core, llm, day, start, end).memory.text == "今天没人来找我"
    assert "没有人来找你说话" in llm.calls[0][1]


def test_previous_diary_is_given_as_context(core):
    day, start, end = _window()
    write_diary(core, FakeLLM("昨天的日记"), day - timedelta(days=1), start - timedelta(days=1), start)
    llm = FakeLLM("今天的日记")
    write_diary(core, llm, day, start, end)
    assert "昨天的日记" in llm.calls[0][1]
    assert [d for d, _ in recent_diaries(core, day)] == [day - timedelta(days=1), day]


def test_self_image_keeps_history(core):
    day, start, end = _window()
    assert update_self_image(core, FakeLLM(), day) is None  # no diary yet
    write_diary(core, FakeLLM("今天学了新梗"), day, start, end)
    llm = FakeLLM("我最近爱学网络梗")
    assert update_self_image(core, llm, day, persona="我是小星") == "我最近爱学网络梗"
    assert "今天学了新梗" in llm.calls[0][1]
    assert core.identity("self-image") == "我最近爱学网络梗\n"
    update_self_image(core, FakeLLM("第二版"), day)
    history = list((core.root / "identity" / "history").glob("self-image-*.md"))
    assert len(history) == 1 and history[0].read_text(encoding="utf-8") == "我最近爱学网络梗\n"
    assert core.identity_names() == ["self-image"]  # history is not an identity document


def test_day_boundary():
    tz = timezone(timedelta(hours=8))
    late = datetime(2026, 10, 30, 2, 30, tzinfo=tz)
    assert current_day(late.astimezone(), 4) == date(2026, 10, 29)
    start, end = day_window(date(2026, 10, 29), 4)
    assert start.hour == 4 and end - start == timedelta(days=1)


def test_extend_keeps_what_was_written_and_adds_what_came_after(core):
    day, start, end = _window()
    _say(core, DM, "owner", "今天去公园了", start + timedelta(hours=10))
    first = write_diary(core, FakeLLM("上午老板去了公园"), day, start, end, note="想记下公园").memory
    llm = FakeLLM("不该被调用")
    assert write_diary(core, llm, day, start, end, extend=True).skipped == "nothing new" and not llm.calls
    _say(core, DM, "owner", "晚上吃了火锅", start + timedelta(hours=16))
    llm = FakeLLM("晚上还吃了火锅")
    more = write_diary(core, llm, day, start, end, extend=True).memory
    assert more.text == "上午老板去了公园\n\n晚上还吃了火锅"
    assert "火锅" in llm.calls[0][1] and "今天去公园了" not in llm.calls[0][1]  # only what came after
    assert "上午老板去了公园" in llm.calls[0][1]  # given as what is already written
    assert core.memory(first.id).status == "superseded" and len(more.sources) == 2


def test_extend_without_a_diary_writes_one_and_note_is_passed(core):
    day, start, end = _window()
    _say(core, DM, "owner", "hi", start + timedelta(hours=1))
    llm = FakeLLM("第一篇")
    assert write_diary(core, llm, day, start, end, extend=True, note="爱吃鱼").memory.text == "第一篇"
    assert "你特别想写进去的：爱吃鱼" in llm.calls[0][1]
