from datetime import datetime, timedelta, timezone

import pytest

from memory_core import Episode, Memory
from memory_core.ids import new_id
from memory_core.text import NgramTokenizer, fts_query, get_tokenizer
from memory_core.timeutil import to_iso


def test_ids_are_unique_sortable_and_26_chars():
    ids = [new_id() for _ in range(2000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    assert all(len(i) == 26 for i in ids)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 1, 1))


def test_ngram_tokenizer_cjk_and_latin():
    toks = NgramTokenizer().tokenize("你是不吃香菜吗 Genshin！")
    assert "香菜" in toks and "香" in toks and "genshin" in toks
    assert "！" not in toks


def test_jieba_tokenizer_optional():
    pytest.importorskip("jieba")
    toks = get_tokenizer("jieba").tokenize("我喜欢原神和明日方舟")
    assert "原神" in toks


def test_fts_query_escapes_quotes():
    assert fts_query(['a"b', "c"]) == '"a""b" OR "c"'
    assert fts_query([]) is None


def test_record_is_append_only_and_resolves_actor(core):
    ep = core.record(Episode(kind="message_in", content="今天打原神", space="qq:group:g1", source="qq",
                             actor="qq:1001"))
    assert ep.id and ep.recorded_at is not None
    person = core.entity("qq:1001")
    assert person is not None and ep.actor == person.id
    # same alias -> same entity
    ep2 = core.record(Episode(kind="message_in", content="再来", space="qq:group:g1", source="qq",
                              actor="qq:1001"))
    assert ep2.actor == person.id
    assert [e.id for e in core.recent("qq:group:g1")] == [ep.id, ep2.id]
    assert not hasattr(core, "update_episode")


def test_record_validates(core):
    with pytest.raises(ValueError):
        core.record(Episode(kind="nonsense", content="x", space="s", source="t"))
    with pytest.raises(ValueError):
        core.record(Episode(kind="message_in", content="x", space="", source="t"))
    core.record(Episode(kind="x-custom", content="x", space="s", source="t"))


def test_ensure_entity_and_aliases(core):
    owner = core.ensure_entity("qq:42", name="小林", aliases=("主人",))
    again = core.ensure_entity("主人", aliases=("小林",))
    assert again.id == owner.id
    assert set(core.entity(owner.id).aliases) == {"qq:42", "主人", "小林"}


def test_rename_entity(core):
    e = core.ensure_entity("self")
    core.rename_entity(e.id, "我")
    assert core.entity("self").name == "我"
    with pytest.raises(KeyError):
        core.rename_entity("nope", "x")


def test_add_memory_validates_sources_and_subjects(core):
    with pytest.raises(KeyError):
        core.add_memory(Memory(kind="fact", text="x", scope="public", sources=["NOPE"]))
    with pytest.raises(KeyError):
        core.add_memory(Memory(kind="fact", text="x", scope="public", subjects=["nobody"]))
    with pytest.raises(ValueError):
        core.add_memory(Memory(kind="fact", text="x", scope="secret"))
    with pytest.raises(ValueError):
        core.add_memory(Memory(kind="fact", text="x", scope="public", importance=2))
    ep = core.record(Episode(kind="message_in", content="我不吃香菜", space="qq:group:g1", source="qq",
                             actor="qq:1001"))
    m = core.add_memory(Memory(kind="preference", text="阿明不吃香菜", scope="space:qq:group:g1",
                               subjects=["qq:1001"], sources=[ep.id]))
    stored = core.memory(m.id)
    assert stored.sources == [ep.id]
    assert stored.subjects == [core.entity("qq:1001").id]
    assert core.stats()["embeddings"] == 1


def test_identity_documents(core):
    assert core.identity("self") is None
    core.set_identity("self", "# 我是谁\n一只好奇的猫\n")
    assert core.identity("self").startswith("# 我是谁")
    assert core.identity_names() == ["self"]
    with pytest.raises(ValueError):
        core.set_identity("../escape", "x")


def test_episode_search_and_time_filter(core):
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    core.record(Episode(kind="message_in", content="恒大爆雷了", space="g", source="qq", occurred_at=t0))
    core.record(Episode(kind="message_in", content="刘强东", space="g", source="qq",
                        occurred_at=t0 + timedelta(days=2)))
    assert [e.content for e in core.search_episodes("恒大")] == ["恒大爆雷了"]
    assert [e.content for e in core.episodes("g", since=t0 + timedelta(days=1))] == ["刘强东"]
