import json
from datetime import datetime, timedelta, timezone

from memory_core import Episode
from memory_core.now import (
    ago, describe, energy, load_mood, longing, mood_due, snapshot, update_mood,
)

DM = "qq:private:owner"
NOW = datetime(2026, 10, 30, 20, 0).astimezone()


class FakeLLM:
    model_id = "fake"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, system, user):
        self.calls.append((system, user))
        a = self.answers.pop(0)
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)


def _say(core, text, when, kind="message_in", space=DM):
    core.record(Episode(kind=kind, content=text, space=space, source="t", occurred_at=when,
                        actor="qq:owner" if kind == "message_in" else None))


def test_curves_and_words():
    assert longing(0) == 0 and abs(longing(12) - 0.5) < 1e-9 and longing(72) > 0.95 and longing(None) == 0
    assert energy(datetime(2026, 1, 1, 3), 0) < 0.2 < energy(datetime(2026, 1, 1, 15), 0)
    assert energy(datetime(2026, 1, 1, 15), 0.5) < energy(datetime(2026, 1, 1, 15), 0)
    assert ago(timedelta(seconds=30)) == "刚刚" and ago(timedelta(minutes=20)) == "20 分钟前"
    assert ago(timedelta(hours=3)) == "3 小时前"
    assert ago(timedelta(days=3), datetime(2026, 10, 27, 21).astimezone()).startswith("3 天前（周二晚上")



def test_snapshot_after_an_absence(core):
    _say(core, "晚安", NOW - timedelta(days=3))
    _say(core, "我回来了", NOW - timedelta(minutes=1))
    s = snapshot(core, owner_spaces=[DM], now=NOW)
    assert s.talking_now and s.last_owner is not None
    assert s.longing > 0.9  # three days away still shows when he just came back
    note = describe(s, owner_present=True, owner_title="老板", now=NOW)
    assert "老板隔了好久才来找你" in note and "3 天前" in note and "想老板：很想" in note
    later = snapshot(core, owner_spaces=[DM], now=NOW + timedelta(minutes=2))
    assert later.longing <= s.longing


def test_quiet_and_privacy(core):
    _say(core, "我去忙了", NOW - timedelta(hours=5))
    s = snapshot(core, owner_spaces=[DM], now=NOW)
    assert not s.talking_now and 0.2 < s.longing < 0.3
    owner = describe(s, owner_present=True, owner_title="老板", now=NOW)
    other = describe(s, owner_present=False, owner_title="老板", now=NOW)
    assert "老板上次来是5 小时前" in owner
    assert "老板" not in other and "现在：" in other and "心情" in other


def test_mood_judged_after_a_quiet_talk(core):
    _say(core, "小星今天好乖", NOW - timedelta(hours=1))
    _say(core, "嘻嘻", NOW - timedelta(minutes=59), kind="message_out")
    assert mood_due(core, [DM], NOW - timedelta(minutes=50)) is None  # still talking
    newest = mood_due(core, [DM], NOW)
    assert newest is not None
    llm = FakeLLM({"value": 0.8, "label": "美滋滋", "text": "被老板夸了"})
    mood = update_mood(core, llm, owner_spaces=[DM], owner_title="老板", now=NOW, until=newest)
    assert mood.label == "美滋滋" and load_mood(core).text == "被老板夸了"
    assert "老板：小星今天好乖" in llm.calls[0][1] and "我：嘻嘻" in llm.calls[0][1]
    assert mood_due(core, [DM], NOW) is None  # already judged
    s = snapshot(core, owner_spaces=[DM], now=NOW)
    assert "心情：美滋滋（被老板夸了）" in describe(s, owner_present=True, owner_title="老板", now=NOW)
    other = describe(s, owner_present=False, owner_title="老板", now=NOW)
    assert "美滋滋" in other and "被老板夸了" not in other  # the reason may be about the owner
    # a day later the mood has faded back towards calm
    s2 = snapshot(core, owner_spaces=[DM], now=NOW + timedelta(days=2))
    assert s2.mood_fresh < 0.1 and "美滋滋" not in describe(s2, owner_present=True, now=NOW + timedelta(days=2))


def test_outings_count_today(core):
    core.record(Episode(kind="observation", content="在B站看到《塔露拉》", space="web", source="web",
                        occurred_at=NOW - timedelta(hours=2), meta={"title": "塔露拉上岛"}))
    s = snapshot(core, owner_spaces=[DM], now=NOW)
    assert s.outings_today == ["塔露拉上岛"] and s.longing is None
    assert "《塔露拉上岛》" in describe(s, owner_present=False)
