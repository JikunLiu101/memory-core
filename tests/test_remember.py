import json
from datetime import timedelta

from memory_core import Audience, Episode, Memory, timeutil
from memory_core.consolidate import due_spaces, remember

DM = "qq:private:owner"
OWNER = Audience(DM, owner_present=True)


class FakeLLM:
    model_id = "fake"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, system, user):
        self.calls.append((system, user))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)


def _m(text, scope="owner", importance=0.5):
    return Memory(kind="event", text=text, scope=scope, importance=importance)


def test_nothing_related_adds_without_asking(core):
    llm = FakeLLM()
    r = remember(core, llm, _m("主人喜欢游泳"), OWNER)
    assert r.op == "add" and llm.calls == [] and core.memory(r.memory.id).status == "active"


def test_outcome_updates_the_plan(core):
    plan = core.add_memory(_m("主人中午要去吃一家拉面", importance=0.35))
    llm = FakeLLM({"op": "update", "target": "E1", "text": "主人中午去吃了一家拉面，很难吃"})
    r = remember(core, llm, _m("主人吃的那家拉面很难吃", importance=0.7), OWNER)
    assert r.op == "update" and r.replaced == [plan.id]
    assert core.memory(plan.id).status == "superseded"
    assert r.memory.text == "主人中午去吃了一家拉面，很难吃" and r.memory.importance == 0.7
    assert "拉面" in llm.calls[0][1] and "E1:" in llm.calls[0][1]


def test_merge_and_skip(core):
    a = core.add_memory(_m("主人中午要去吃拉面"))
    b = core.add_memory(_m("主人吃的拉面很难吃，3/10"))
    r = remember(core, FakeLLM({"op": "merge", "targets": ["E1", "E2"], "text": "主人中午吃的拉面很难吃，3/10"}),
                 _m("那家拉面主人给了3/10"), OWNER)
    assert r.op == "merge" and set(r.replaced) == {a.id, b.id}
    r2 = remember(core, FakeLLM({"op": "skip", "target": "E1"}), _m("主人中午吃的拉面很难吃"), OWNER)
    assert r2.op == "skip" and r2.memory.id == r.memory.id
    assert len([m for m in [core.memory(r.memory.id)] if m.status == "active"]) == 1


def test_only_same_scope_is_considered(core):
    core.add_memory(_m("主人要去吃拉面", scope="public"))
    llm = FakeLLM()
    r = remember(core, llm, _m("主人吃的拉面很难吃"), OWNER)
    assert r.op == "add" and llm.calls == []  # a public memory is never updated from a private chat


def test_model_failure_still_remembers(core):
    core.add_memory(_m("主人要去吃拉面"))
    for answer in (TimeoutError("down"), "not json", {"op": "update", "target": "E9"}):
        r = remember(core, FakeLLM(answer), _m(f"主人吃的拉面很难吃 {answer!s:.3}"), OWNER)
        assert r.op == "add" and core.memory(r.memory.id).status == "active"


def test_no_model_just_adds(core):
    core.add_memory(_m("主人要去吃拉面"))
    assert remember(core, None, _m("主人吃的拉面很难吃"), OWNER).op == "add"


def test_due_spaces_includes_quiet_chats(core):
    now = timeutil.now()
    for i in range(10):
        core.record(Episode(kind="message_in", content=f"群消息{i}", space="qq:group:1", source="qq"))
    core.record(Episode(kind="message_in", content="刚说的", space=DM, source="qq"))
    core.record(Episode(kind="message_in", content="早就说了", space="qq:private:2", source="qq",
                        occurred_at=now - timedelta(minutes=90)))
    assert set(due_spaces(core, min_new=10, idle_minutes=60, now=now)) == {"qq:group:1", "qq:private:2"}
    assert set(due_spaces(core, min_new=10, idle_minutes=0, now=now)) == {"qq:group:1"}
    later = now + timedelta(minutes=61)
    assert set(due_spaces(core, min_new=10, idle_minutes=60, now=later)) == {"qq:group:1", "qq:private:2", DM}
