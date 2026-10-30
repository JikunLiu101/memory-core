import json

import pytest

from memory_core import Audience, Episode, MemoryCore
from memory_core.consolidate import SYSTEM_PROMPT
from memory_core.llm import parse_json

G1 = "qq:group:g1"
DM = "qq:private:owner"


class FakeLLM:
    model_id = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


def _chat(core, space, lines):
    eps = []
    for actor, text in lines:
        kind = "message_out" if actor == "self" else "message_in"
        eps.append(core.record(Episode(kind=kind, content=text, space=space, source="qq", actor=actor)))
    return eps


def test_add_with_labels_resolved_and_scope_kept_in_space(core):
    core.ensure_entity("qq:1001", name="阿明")
    _chat(core, G1, [("qq:1001", "我不吃香菜"), ("self", "那你很挑食")])
    llm = FakeLLM({"memories": [
        {"op": "add", "kind": "preference", "text": "阿明不吃香菜", "subjects": ["P1"], "evidence": ["M1"],
         "importance": 0.6, "visibility": "public"},  # model asks for public; code must refuse (about a person)
        {"op": "add", "kind": "lexicon", "text": "“不吃香菜”可以指不接受某样东西", "subjects": [],
         "visibility": "public"},
    ]})
    report = core.consolidate(llm)
    assert report.episodes == 2 and len(report.added) == 2
    pref, lex = (core.memory(i) for i in report.added)
    assert pref.scope == f"space:{G1}"
    assert pref.subjects == [core.entity("qq:1001").id]
    assert len(pref.sources) == 1 and core.episode(pref.sources[0]).content == "我不吃香菜"
    assert pref.derived_by == "fake/consolidate-v2"
    assert lex.scope == "public"
    system, user = llm.calls[0]
    assert system == SYSTEM_PROMPT
    assert "P1: 阿明" in user and "我: 那你很挑食" in user


def test_cursor_advances_and_nothing_is_processed_twice(core):
    _chat(core, G1, [("qq:1", "第一句")])
    llm = FakeLLM({"memories": []}, {"memories": []})
    assert core.consolidate(llm).episodes == 1
    assert core.pending_consolidation() == {}
    assert core.consolidate(llm).episodes == 0
    _chat(core, G1, [("qq:1", "第二句")])
    assert core.pending_consolidation() == {G1: 1}
    assert core.consolidate(llm).episodes == 1
    assert len(llm.calls) == 2


def test_failed_llm_call_keeps_cursor(core):
    _chat(core, G1, [("qq:1", "hello")])
    with pytest.raises(json.JSONDecodeError):
        core.consolidate(FakeLLM("not json"))
    assert core.pending_consolidation() == {G1: 1}


def test_update_supersedes_existing_memory(core):
    core.ensure_entity("qq:1001", name="阿明")
    _chat(core, G1, [("qq:1001", "我住北京")])
    core.consolidate(FakeLLM({"memories": [
        {"op": "add", "kind": "fact", "text": "阿明住在北京", "subjects": ["P1"]}]}))
    _chat(core, G1, [("qq:1001", "我搬到上海了")])
    llm = FakeLLM({"memories": [
        {"op": "update", "target": "E1", "kind": "fact", "text": "阿明搬到了上海", "subjects": ["P1"]}]})
    report = core.consolidate(llm)
    assert "E1: 阿明住在北京" in llm.calls[0][1]
    (old_id, new_id), = report.updated
    assert core.memory(old_id).status == "superseded"
    assert core.memory(new_id).text == "阿明搬到了上海"


def test_owner_private_chat_memories_are_owner_scoped(core):
    core.ensure_entity("qq:42", name="主人")
    _chat(core, DM, [("qq:42", "我其实很怕蛇")])
    report = core.consolidate(FakeLLM({"memories": [
        {"op": "add", "kind": "fact", "text": "主人怕蛇", "subjects": ["P1"], "visibility": "public"},
        {"op": "add", "kind": "preference", "text": "我喜欢听主人讲故事", "subjects": ["我"], "visibility": "self"},
    ]}), owner_spaces=[DM])
    a, b = (core.memory(i) for i in report.added)
    assert a.scope == "owner" and b.scope == "self"
    assert a.id not in {h.memory.id for h in core.recall("蛇", Audience(G1))}
    assert a.id in {h.memory.id for h in core.recall("蛇", Audience(DM, owner_present=True))}


def test_bad_items_are_rejected_not_fatal(core):
    _chat(core, G1, [("qq:1", "x")])
    report = core.consolidate(FakeLLM({"memories": [
        {"op": "add", "kind": "identity", "text": "我是神"},
        {"op": "add", "kind": "fact", "text": ""},
        {"op": "update", "target": "E9", "kind": "fact", "text": "x"},
        {"op": "add", "kind": "fact", "text": "有效的一条", "subjects": [{"name": "原神", "type": "work"}]},
    ]}))
    assert len(report.rejected) == 3 and len(report.added) == 1
    assert core.entity("原神").kind == "work"


def test_spaces_are_consolidated_separately(core):
    _chat(core, G1, [("qq:1", "一群的话")])
    _chat(core, "qq:group:g2", [("qq:2", "二群的话")])
    llm = FakeLLM({"memories": []}, {"memories": []})
    report = core.consolidate(llm)
    assert sorted(report.spaces) == [G1, "qq:group:g2"]
    assert all("一群的话" not in u or "二群的话" not in u for _, u in llm.calls)


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"memories": []}\n```') == {"memories": []}


def test_consolidate_needs_llm(tmp_path):
    with MemoryCore.open(tmp_path / "s", embedder=None) as c:
        with pytest.raises(RuntimeError):
            c.consolidate()


def test_what_it_saw_online_is_public_what_it_thought_is_owners(core):
    core.ensure_entity("self", name="我")
    core.record(Episode(kind="observation", content="在B站看到《星露谷新版本》", space="web", source="web"))
    core.record(Episode(kind="thought", content="想和主人一起玩", space="web", source="web", actor="self"))
    llm = FakeLLM({"memories": [
        {"op": "add", "kind": "event", "text": "我在B站看到星露谷出了新版本", "subjects": [], "evidence": ["M1"]},
        {"op": "add", "kind": "preference", "text": "我想和主人一起玩星露谷", "subjects": ["我"],
         "evidence": ["M1", "M2"], "visibility": "self"},
        {"op": "add", "kind": "fact", "text": "星露谷是农场游戏", "subjects": []},  # no evidence: all of them
    ]})
    report = core.consolidate(llm)
    saw, felt, unsure = (core.memory(i) for i in report.added)
    assert saw.scope == "public" and felt.scope == "owner" and unsure.scope == "owner"
    assert not core.recall("一起玩", Audience(G1), reinforce=False)  # not in a group
    assert "(public:" in llm.calls[0][1]
    # it comes up in a private chat as well
    assert core.recall("星露谷", Audience(DM, owner_present=True), reinforce=False)


def test_merge_plan_and_outcome(core):
    from memory_core import Memory
    plan = core.add_memory(Memory(kind="event", text="主人中午要去吃一家拉面", scope="owner", importance=0.35))
    outcome = core.add_memory(Memory(kind="event", text="主人吃的拉面很难吃，3/10", scope="owner", importance=0.7))
    _chat(core, DM, [("qq:owner", "那家拉面满分5分我顶多给2分")])
    llm = FakeLLM({"memories": [
        {"op": "merge", "targets": ["E1", "E2"], "kind": "event", "evidence": ["M1"],
         "text": "主人中午去吃了一家拉面，很难吃：3/10，满分5分顶多2分"},
    ]})
    report = core.consolidate(llm, owner_spaces=[DM])
    assert not report.rejected and not report.added
    new_id = report.updated[0][1]
    merged = core.memory(new_id)
    assert merged.scope == "owner" and merged.importance == 0.7  # kept from the originals
    assert core.memory(plan.id).status == "superseded" and core.memory(outcome.id).status == "superseded"
    assert len(merged.sources) == 1  # the new evidence
    hits = core.recall("拉面", Audience(DM, owner_present=True), reinforce=False)
    assert [h.memory.id for h in hits] == [new_id]
    assert "turned out" in llm.calls[0][0] and '"merge"' in llm.calls[0][0]


def test_merge_needs_two_targets_and_one_scope(core):
    from memory_core import Memory
    core.add_memory(Memory(kind="fact", text="阿明喜欢猫", scope=f"space:{G1}"))
    core.add_memory(Memory(kind="fact", text="阿明喜欢猫咪", scope="public"))
    _chat(core, G1, [("qq:1001", "我喜欢猫")])
    llm = FakeLLM({"memories": [
        {"op": "merge", "targets": ["E1"], "text": "x"},
        {"op": "merge", "targets": ["E1", "E2"], "text": "阿明喜欢猫"},
    ]})
    report = core.consolidate(llm)
    assert len(report.rejected) == 2 and not report.updated
