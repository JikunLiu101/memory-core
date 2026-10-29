from datetime import datetime, timedelta, timezone

import pytest

from memory_core import Audience, HashEmbedder, Memory, MemoryCore

G1 = "qq:group:g1"
G2 = "qq:group:g2"
DM = "qq:private:owner"


def _seed(core):
    core.ensure_entity("qq:1001", name="阿明")
    return {
        "g1": core.add_memory(Memory(kind="preference", text="阿明不吃香菜", scope=f"space:{G1}",
                                     subjects=["qq:1001"])),
        "g2": core.add_memory(Memory(kind="fact", text="二群的香菜梗是指挑食", scope=f"space:{G2}")),
        "owner": core.add_memory(Memory(kind="fact", text="主人最讨厌香菜", scope="owner")),
        "public": core.add_memory(Memory(kind="lexicon", text="不吃香菜：网络梗，表示不接受某样东西",
                                         scope="public")),
        "self": core.add_memory(Memory(kind="preference", text="我自己其实喜欢香菜", scope="self")),
    }


def _ids(hits):
    return {h.memory.id for h in hits}


def test_scope_is_enforced_in_every_channel(core):
    m = _seed(core)
    hits = core.recall("香菜", Audience(G1), k=20)
    assert _ids(hits) == {m["g1"].id, m["public"].id, m["self"].id}
    hits = core.recall("香菜", Audience(G2), k=20)
    assert _ids(hits) == {m["g2"].id, m["public"].id, m["self"].id}
    hits = core.recall("香菜", Audience(DM, owner_present=True), k=20)
    assert _ids(hits) == {m["owner"].id, m["public"].id, m["self"].id}


def test_scope_and_recall_with_jieba(tmp_path):
    pytest.importorskip("jieba")
    with MemoryCore.open(tmp_path / "j", embedder=None, tokenizer="jieba") as c:
        m = _seed(c)
        assert _ids(c.recall("不吃香菜是什么梗", Audience(G2), k=20)) == {m["g2"].id, m["public"].id, m["self"].id}


def test_scope_enforced_text_only(text_core):
    m = _seed(text_core)
    assert m["owner"].id not in _ids(text_core.recall("香菜", Audience(G1), k=20))


def test_entity_channel_finds_memories_about_mentioned_people(text_core):
    m = _seed(text_core)
    hits = text_core.recall("阿明最近怎么样", Audience(G1), k=5)
    by_id = {h.memory.id: h for h in hits}
    assert m["g1"].id in by_id and "entity" in by_id[m["g1"].id].channels


def test_inactive_and_expired_memories_are_not_recalled(core):
    m = _seed(core)
    core.set_status(m["public"].id, "archived")
    assert m["public"].id not in _ids(core.recall("香菜", Audience(G1), k=20))
    assert m["public"].id in _ids(core.recall("香菜", Audience(G1), k=20, include_archived=True))
    past = datetime.now(timezone.utc) - timedelta(days=1)
    expired = core.add_memory(Memory(kind="fact", text="香菜限时活动", scope="public", valid_to=past))
    assert expired.id not in _ids(core.recall("香菜", Audience(G1), k=20))


def test_supersede_keeps_history(core):
    old = core.add_memory(Memory(kind="fact", text="阿明住在北京", scope="public"))
    new = core.supersede(old.id, Memory(kind="fact", text="阿明搬到上海了", scope="public"))
    old_now = core.memory(old.id)
    assert old_now.status == "superseded" and old_now.valid_to is not None
    assert old_now.meta["superseded_by"] == new.id
    assert core.memory(new.id).meta["supersedes"] == old.id
    assert old.id not in _ids(core.recall("阿明 北京 上海", Audience(G1), k=10))


def test_recall_reinforces_strength(core):
    m = core.add_memory(Memory(kind="fact", text="原神新版本", scope="public", strength=0.5))
    core.recall("原神", Audience(G1))
    after = core.memory(m.id)
    assert after.strength > 0.5 and after.last_recalled_at is not None
    core.recall("原神", Audience(G1), reinforce=False)
    assert core.memory(m.id).strength == after.strength


def test_vector_channel_used_and_scored(core):
    _seed(core)
    hits = core.recall("香菜", Audience(G1), k=3)
    assert any("vector" in h.channels for h in hits)
    assert hits == sorted(hits, key=lambda h: -h.score)
    assert all(0 < h.relevance <= 1 for h in hits)


def test_no_match_returns_nothing(core):
    _seed(core)
    assert core.recall("量子力学", Audience(G1)) == []


class _Broken:
    model_id = "broken"

    def embed(self, texts):
        raise ConnectionError("embedding server down")


def test_embedder_failure_degrades_gracefully(tmp_path):
    with MemoryCore.open(tmp_path / "s", embedder=_Broken(), tokenizer="ngram") as c:
        m = c.add_memory(Memory(kind="fact", text="香菜", scope="public"))  # embedding fails, memory kept
        assert c.stats()["embeddings"] == 0
        assert _ids(c.recall("香菜", Audience(G1))) == {m.id}


def test_reindex_with_a_new_model_keeps_old_vectors(core):
    _seed(core)
    new_model = HashEmbedder(64)
    assert core.reindex(new_model) == 5
    assert core.reindex(new_model) == 0
    assert core.stats()["embedding_models"] == {"hash-v1-256": 5, "hash-v1-64": 5}
    core.embedder = new_model
    assert any("vector" in h.channels for h in core.recall("香菜", Audience(G1)))


def test_tokenizer_change_rebuilds_index(tmp_path):
    with MemoryCore.open(tmp_path / "s", embedder=None, tokenizer="ngram") as c:
        c.add_memory(Memory(kind="fact", text="明日方舟", scope="public"))

    class Upper:
        id = "custom-test"

        def tokenize(self, text):
            return [ch for ch in text]

    import memory_core.core as core_mod

    original = core_mod.get_tokenizer
    core_mod.get_tokenizer = lambda name: Upper()
    try:
        with MemoryCore.open(tmp_path / "s", embedder=None) as c:
            assert c.store.get_meta("tokenizer") == "custom-test"
            assert len(c.recall("方", Audience(G1))) == 1
    finally:
        core_mod.get_tokenizer = original
