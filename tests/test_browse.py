"""Read-only views for the owner's tools: they show everything, and change nothing."""

from memory_core import Audience, Episode, Memory, MemoryStatus
from memory_core import browse

DM = "qq:private:owner"
G1 = "qq:group:g1"


def _seed(core):
    core.ensure_entity("owner", name="主人")
    core.ensure_entity("qq:1001", name="小星")
    core.ensure_entity("塞尔达", kind="work")
    said = core.record(Episode(kind="message_in", content="我和小星周末一起玩塞尔达", space=DM, source="qq",
                               actor="owner"))
    core.record(Episode(kind="message_in", content="今天好热", space=G1, source="qq", actor="qq:1001"))
    play = core.add_memory(Memory(kind="event", text="主人和小星周末一起玩塞尔达", scope="owner",
                                  subjects=["owner", "qq:1001", "塞尔达"], sources=[said.id], importance=0.7))
    likes = core.add_memory(Memory(kind="preference", text="小星喜欢塞尔达", scope=f"space:{G1}",
                                   subjects=["qq:1001", "塞尔达"]))
    hot = core.add_memory(Memory(kind="fact", text="小星怕热", scope=f"space:{G1}", subjects=["qq:1001"]))
    newer = core.supersede(hot.id, Memory(kind="fact", text="小星很怕热，夏天不出门", scope=f"space:{G1}",
                                          subjects=["qq:1001"]))
    faded = core.add_memory(Memory(kind="fact", text="主人以前养过一只猫", scope="owner", subjects=["owner"]))
    core.set_status(faded.id, MemoryStatus.ARCHIVED)
    return {"play": play, "likes": likes, "hot": hot, "newer": newer, "faded": faded, "said": said}


def test_list_filters(core):
    m = _seed(core)
    every = browse.list_memories(core)
    assert every["total"] == 5  # superseded and archived are listed too
    assert {i["id"] for i in browse.list_memories(core, statuses=["active"])["items"]} == {
        m["play"].id, m["likes"].id, m["newer"].id}
    assert [i["id"] for i in browse.list_memories(core, text="猫")["items"]] == [m["faded"].id]
    assert {i["id"] for i in browse.list_memories(core, scope="space:*")["items"]} == {
        m["likes"].id, m["hot"].id, m["newer"].id}
    work = core.entity("塞尔达")
    assert {i["id"] for i in browse.list_memories(core, entity=work.id)["items"]} == {m["play"].id, m["likes"].id}
    page = browse.list_memories(core, limit=2, offset=1)
    assert page["total"] == 5 and len(page["items"]) == 2
    item = next(i for i in every["items"] if i["id"] == m["play"].id)
    assert {s["name"] for s in item["subjects"]} == {"主人", "小星", "塞尔达"}
    assert item["source_count"] == 1


def test_detail_has_sources_and_versions(core):
    m = _seed(core)
    d = browse.memory_detail(core, m["play"].id)
    assert [s["content"] for s in d["sources"]] == ["我和小星周末一起玩塞尔达"]
    assert d["sources"][0]["actor"] == "主人"
    d = browse.memory_detail(core, m["newer"].id)
    assert [v["text"] for v in d["history"]] == ["小星怕热", "小星很怕热，夏天不出门"]
    assert d["history"][0]["status"] == "superseded"
    assert browse.memory_detail(core, "nope") is None


def test_explain_recall_changes_nothing(core):
    m = _seed(core)
    before = {k: core.memory(v.id) for k, v in m.items() if k != "said"}
    out = browse.explain_recall(core, "小星 塞尔达", Audience(DM, owner_present=True), include_archived=True)
    assert out["hits"], out
    hit = out["hits"][0]
    assert set(hit["parts"]) == {"relevance", "recency", "importance", "strength"}
    assert hit["channels"] and all(isinstance(r, int) and r >= 1 for r in hit["channels"].values())
    assert "小星" in out["mentioned"]
    for k, old in before.items():
        now = core.memory(old.id)
        assert (now.strength, now.last_recalled_at, now.status) == (old.strength, old.last_recalled_at, old.status), k


def test_explain_recall_respects_the_audience(core):
    m = _seed(core)
    group = {h["memory"]["id"] for h in browse.explain_recall(core, "小星", Audience(G1))["hits"]}
    owner = {h["memory"]["id"] for h in browse.explain_recall(core, "小星", Audience(DM, owner_present=True))["hits"]}
    assert m["play"].id not in group  # the owner's memory never shows in the group
    assert m["play"].id in owner and m["likes"].id not in owner


def test_graph(core):
    m = _seed(core)
    g = browse.graph(core)
    names = {e["name"]: e for e in g["entities"]}
    assert set(names) == {"主人", "小星", "塞尔达"}
    assert names["小星"]["count"] == 3
    assert {x["id"] for x in g["memories"]} == {m["play"].id, m["likes"].id, m["newer"].id}
    star, zelda = names["小星"]["id"], names["塞尔达"]["id"]
    weights = {(p["a"], p["b"]): p["weight"] for p in g["pairs"]}
    assert weights[tuple(sorted((star, zelda)))] == 2
    archived = browse.graph(core, statuses=["archived"])
    assert [x["id"] for x in archived["memories"]] == [m["faded"].id]
    groups = browse.graph(core, scope="space:*")
    assert {x["id"] for x in groups["memories"]} == {m["likes"].id, m["newer"].id}


def test_entity_and_overview(core):
    _seed(core)
    star = core.entity("小星")
    d = browse.entity_detail(core, star.id)
    assert d["entity"]["name"] == "小星" and "qq:1001" in d["entity"]["aliases"]
    assert [x["text"] for x in d["memories"]][0] == "主人和小星周末一起玩塞尔达"  # most important first
    o = browse.overview(core)
    assert o["counts"]["memories"] == 5
    assert o["pending"] == {DM: 1, G1: 1}
    assert {s["space"] for s in o["spaces"]} == {DM, G1}
    assert browse.spaces(core)[0] in (DM, G1)
