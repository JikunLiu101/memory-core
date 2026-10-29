import json

import pytest

from memory_core import Audience, Episode, HashEmbedder, Memory, MemoryCore
from memory_core.bundle import BundleError, import_bundle, read_manifest
from memory_core.cli import main


def _fill(core):
    core.ensure_entity("qq:42", name="小林", aliases=("主人",))
    e1 = core.record(Episode(kind="message_in", content="今天看了 B 站", space="qq:private:42", source="qq",
                             actor="qq:42"))
    e2 = core.record(Episode(kind="observation", content="一个讲恒大的视频", space="web", source="browser",
                             meta={"url": "https://example.com/v"}))
    core.add_memory(Memory(kind="event", text="主人推荐我看了恒大的视频", scope="owner",
                           subjects=["主人"], sources=[e1.id, e2.id], importance=0.8))
    core.set_identity("self", "# self\n好奇\n")
    (core.root / "attachments" / "a.txt").write_text("hello", encoding="utf-8")


def test_roundtrip_preserves_everything(core, tmp_path):
    _fill(core)
    manifest = core.export_bundle(tmp_path / "bundle")
    assert manifest["counts"] == {"entities": 1, "episodes": 2, "memories": 1, "identity": 1}
    assert not any("vector" in name for name in manifest["files"])

    restored = import_bundle(tmp_path / "bundle", tmp_path / "restored", embedder=None, tokenizer="ngram")
    with restored:
        assert restored.stats()["episodes"] == 2
        assert restored.stats()["embeddings"] == 0
        assert restored.identity("self") == "# self\n好奇\n"
        assert (restored.root / "attachments" / "a.txt").read_text(encoding="utf-8") == "hello"
        orig_eps = {e.id: e for e in core.episodes()}
        for ep in restored.episodes():
            assert ep == orig_eps[ep.id]
        orig_mem = core.store.get_memories(core.store.iter_memory_ids())
        new_mem = restored.store.get_memories(restored.store.iter_memory_ids())
        assert orig_mem == new_mem
        # vectors are rebuilt with whatever model is current
        assert restored.reindex(HashEmbedder(32)) == 1
        hits = restored.recall("恒大", Audience("qq:private:42", owner_present=True))
        assert len(hits) == 1


def test_bundle_is_plain_text_jsonl(core, tmp_path):
    _fill(core)
    core.export_bundle(tmp_path / "b")
    lines = (tmp_path / "b" / "memories.jsonl").read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[0])
    assert record["text"] == "主人推荐我看了恒大的视频"  # readable, not escaped
    assert "主人" in lines[0]
    assert sorted(p.name for p in (tmp_path / "b" / "episodes").iterdir()) == [
        f"{core.episodes()[0].occurred_at.strftime('%Y-%m')}.jsonl"]


def test_tampering_is_detected(core, tmp_path):
    _fill(core)
    core.export_bundle(tmp_path / "b")
    path = tmp_path / "b" / "memories.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("恒大", "万达"), encoding="utf-8")
    with pytest.raises(BundleError, match="changed"):
        read_manifest(tmp_path / "b")


def test_refuses_non_empty_targets(core, tmp_path):
    _fill(core)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("x")
    with pytest.raises(BundleError):
        core.export_bundle(tmp_path / "b")
    core.export_bundle(tmp_path / "ok")
    with pytest.raises(BundleError):
        import_bundle(tmp_path / "ok", core.root, embedder=None)


def test_newer_format_rejected(core, tmp_path):
    core.export_bundle(tmp_path / "b")
    mf = tmp_path / "b" / "manifest.json"
    data = json.loads(mf.read_text(encoding="utf-8"))
    data["format_version"] = 999
    mf.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BundleError, match="newer"):
        read_manifest(tmp_path / "b")


def test_cli_smoke(tmp_path, capsys):
    root = str(tmp_path / "soul")
    assert main(["init", root]) == 0
    assert (tmp_path / "soul" / "config.toml").exists()
    assert main(["record", root, "--space", "web", "--content", "看了一个视频"]) == 0
    assert main(["remember", root, "--scope", "public", "--text", "原神是一个游戏"]) == 0
    capsys.readouterr()
    assert main(["recall", root, "原神", "--space", "web"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["text"] == "原神是一个游戏"
    assert main(["export", root, str(tmp_path / "bundle")]) == 0
    assert main(["verify", str(tmp_path / "bundle")]) == 0
    assert main(["import", str(tmp_path / "bundle"), str(tmp_path / "copy")]) == 0
    assert main(["remember", root, "--scope", "nope", "--text", "x"]) == 1
    with MemoryCore.open(tmp_path / "copy", embedder=None) as c:
        assert c.stats()["memories"] == 1
