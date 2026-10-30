import json
from datetime import date, datetime, timedelta, timezone

import pytest

from memory_core import Audience, Episode, Memory
from memory_core.metabolism import fade, merge_duplicates, similar_groups
from memory_core.proposals import find, proposals, propose, resolve
from memory_core.reflect import diary_for, write_diary

DM = "webchat:private:owner"
OWNER = Audience(DM, owner_present=True)


class FakeLLM:
    model_id = "fake"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        a = self.answers.pop(0)
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)


def _mem(core, text, **kw):
    kw.setdefault("scope", "owner")
    return core.add_memory(Memory(kind=kw.pop("kind", "fact"), text=text, **kw))


def test_strength_fades_and_recall_revives(core):
    m = _mem(core, "主人喜欢游泳", importance=0.3)
    later = datetime.now(timezone.utc) + timedelta(days=90)
    assert core.effective_strength(core.memory(m.id), later) < 0.15
    keep = _mem(core, "主人的生日是三月", importance=0.9)
    report = fade(core, now=later)
    assert report.archived == [m.id]
    assert core.memory(keep.id).status == "active"
    # not brought up by itself any more...
    assert not core.recall("游泳", OWNER, reinforce=False, now=later)
    # ...but a deliberate search finds it and makes it active again
    hits = core.recall("游泳", OWNER, include_archived=True, now=later)
    assert hits[0].memory.id == m.id
    assert core.memory(m.id).status == "active"


def test_fade_spares_young_and_diary(core):
    young = _mem(core, "刚学会的梗", importance=0.1)
    assert fade(core).archived == []
    later = datetime.now(timezone.utc) + timedelta(days=200)
    start = datetime(2026, 10, 29, 4, tzinfo=timezone.utc)
    core.record(Episode(kind="message_in", content="hi", space=DM, source="t", occurred_at=start))
    diary = write_diary(core, FakeLLM("今天很开心"), date(2026, 10, 29), start, start + timedelta(days=1)).memory
    report = fade(core, now=later)
    assert young.id in report.archived and diary.id not in report.archived


def test_merge_duplicates(core):
    a = _mem(core, "小星第一次醒来叫了主人", importance=0.6)
    b = _mem(core, "小星第一次醒来叫了主人", importance=0.8)
    other = _mem(core, "主人喜欢明日方舟")
    assert similar_groups(core) == [sorted([a.id, b.id])]
    llm = FakeLLM({"same": True, "text": "2026-10-29 小星第一次醒来，叫了主人", "importance": 0.9})
    report = merge_duplicates(core, llm)
    (olds, new_id), = report.merged
    merged = core.memory(new_id)
    assert merged.importance == 0.9 and set(merged.meta["merged_from"]) == {a.id, b.id}
    assert core.memory(a.id).status == "superseded" and core.memory(other.id).status == "active"
    assert merge_duplicates(core, FakeLLM()).groups == 0  # nothing similar left


def test_kept_apart_is_not_asked_again(core):
    _mem(core, "主人周四游泳")
    _mem(core, "主人周四游泳")
    assert merge_duplicates(core, FakeLLM({"same": False})).kept == 1
    assert merge_duplicates(core, FakeLLM()).groups == 0


def test_merge_rejects_mixed_scopes(core):
    a = _mem(core, "x")
    b = _mem(core, "x", scope="public")
    with pytest.raises(ValueError):
        core.merge([a.id, b.id], "x")


def test_forget_follows_versions(core):
    old = _mem(core, "主人住在北京")
    new = core.supersede(old.id, Memory(kind="fact", text="主人搬到了上海", scope="owner"))
    report = core.forget([new.id])
    assert set(report["memories"]) == {old.id, new.id}
    assert core.memory(old.id).text == "" and core.memory(new.id).status == "forgotten"
    assert not core.recall("北京", OWNER, include_archived=True)
    assert not core.recall("上海", OWNER, include_archived=True)
    assert core.stats()["embeddings"] == 0


def test_forget_with_sources_erases_episodes_and_derived_diary(core):
    start = datetime(2026, 10, 29, 4, tzinfo=timezone.utc)
    secret = core.record(Episode(kind="message_in", content="我的银行在招商", space=DM, source="t",
                                 occurred_at=start + timedelta(hours=1)))
    core.record(Episode(kind="message_in", content="今天天气好", space=DM, source="t",
                        occurred_at=start + timedelta(hours=2)))
    m = _mem(core, "主人用招商银行", sources=[secret.id])
    write_diary(core, FakeLLM("爸爸说了银行的事"), date(2026, 10, 29), start, start + timedelta(days=1))
    assert (core.root / "diary" / "2026-10-29.md").exists()
    report = core.forget([m.id], with_sources=True)
    assert report["episodes"] == [secret.id] and report["diaries"] == ["2026-10-29"]
    assert core.store.get_episode(secret.id).content == ""
    assert not core.search_episodes("招商")
    assert core.search_episodes("天气")  # other experiences of that day survive
    assert diary_for(core, date(2026, 10, 29)) is None
    assert not (core.root / "diary" / "2026-10-29.md").exists()
    # the day can be written again from what is left
    llm = FakeLLM("今天天气好")
    write_diary(core, llm, date(2026, 10, 29), start, start + timedelta(days=1))
    assert "招商" not in llm.calls[0][1]


def test_proposals(core):
    core.set_identity("self", "# 核心身份\n我是小星\n")
    p = propose(core, "我想给自己取名叫星澜", reason="攒够阅历了")
    assert [x.id for x in proposals(core)] == [p.id]
    assert find(core, p.id[-6:]).id == p.id
    resolve(core, p.id, approve=True, section="后来的补充（经老板同意）")
    doc = core.identity("self")
    assert doc.startswith("# 核心身份\n我是小星\n") and "## 后来的补充（经老板同意）" in doc and "星澜" in doc
    assert list((core.root / "identity" / "history").glob("self-*.md"))
    with pytest.raises(ValueError):
        resolve(core, p.id, approve=False)
    q = propose(core, "删掉底线")
    resolve(core, q.id, approve=False)
    assert "删掉底线" not in core.identity("self") and proposals(core) == []
    with pytest.raises(ValueError):
        propose(core, "x", target="self-image")
