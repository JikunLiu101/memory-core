"""Soul bundle: the portable, plain-text export format. See SPEC.md.

Vectors are deliberately NOT exported: they belong to one embedding model and are rebuilt after import.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__, timeutil
from .models import Entity, Episode, Memory
from .text import fts_document

if TYPE_CHECKING:
    from .core import MemoryCore

FORMAT = "memory-core-bundle"
FORMAT_VERSION = 1


class BundleError(ValueError):
    pass


def _iso(value) -> str | None:
    return timeutil.to_iso(value) if value is not None else None


def _line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"


def _entity_record(e: Entity) -> dict:
    return {"id": e.id, "kind": e.kind, "name": e.name, "aliases": sorted(e.aliases), "meta": e.meta,
            "created_at": _iso(e.created_at)}


def _episode_record(ep: Episode) -> dict:
    return {"id": ep.id, "kind": ep.kind, "source": ep.source, "space": ep.space, "actor": ep.actor,
            "content": ep.content, "occurred_at": _iso(ep.occurred_at), "recorded_at": _iso(ep.recorded_at),
            "meta": ep.meta}


def _memory_record(m: Memory) -> dict:
    record = asdict(m)
    for key in ("valid_from", "valid_to", "created_at", "updated_at", "last_recalled_at"):
        record[key] = _iso(getattr(m, key))
    return record


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _require_empty_dir(path: Path, what: str) -> None:
    if path.exists() and any(path.iterdir()):
        raise BundleError(f"{what} {path} must be empty or not exist")
    path.mkdir(parents=True, exist_ok=True)


def export_bundle(core: MemoryCore, dest: Path) -> dict:
    _require_empty_dir(dest, "bundle directory")
    files: list[Path] = []

    def write_lines(rel: str, records) -> int:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for record in records:
                fh.write(_line(record))
                n += 1
        files.append(path)
        return n

    counts: dict[str, int] = {}
    counts["entities"] = write_lines("entities.jsonl", (_entity_record(e) for e in core.store.iter_entities()))

    by_month: dict[str, list[dict]] = {}
    for ep in core.store.list_episodes():
        record = _episode_record(ep)
        by_month.setdefault(record["occurred_at"][:7], []).append(record)
    counts["episodes"] = 0
    for month, records in sorted(by_month.items()):
        counts["episodes"] += write_lines(f"episodes/{month}.jsonl", records)

    ids = core.store.iter_memory_ids()
    memories = core.store.get_memories(ids)
    counts["memories"] = write_lines("memories.jsonl", (_memory_record(memories[i]) for i in ids))

    for sub in ("identity", "attachments"):
        src = core.root / sub
        if src.exists():
            for path in sorted(p for p in src.rglob("*") if p.is_file() and not p.name.endswith(".tmp")):
                target = dest / sub / path.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                files.append(target)
    counts["identity"] = len(list((dest / "identity").glob("*.md"))) if (dest / "identity").exists() else 0

    manifest = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generator": f"memory-core {__version__}",
        "exported_at": timeutil.to_iso(timeutil.now()),
        "counts": counts,
        "files": {p.relative_to(dest).as_posix(): _sha256(p) for p in sorted(files)},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                                        + "\n", encoding="utf-8", newline="\n")
    return manifest


def read_manifest(src: Path, *, verify: bool = True) -> dict:
    path = src / "manifest.json"
    if not path.exists():
        raise BundleError(f"{src} is not a bundle (manifest.json missing)")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise BundleError(f"unknown bundle format {manifest.get('format')!r}")
    if not isinstance(manifest.get("format_version"), int) or manifest["format_version"] > FORMAT_VERSION:
        raise BundleError(f"bundle format_version {manifest.get('format_version')} is newer than supported "
                          f"({FORMAT_VERSION}); upgrade memory-core")
    if verify:
        for rel, digest in manifest.get("files", {}).items():
            file = src / rel
            if not file.is_file():
                raise BundleError(f"bundle file missing: {rel}")
            if _sha256(file) != digest:
                raise BundleError(f"bundle file changed or corrupted: {rel}")
    return manifest


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def import_bundle(src: str | Path, dest: str | Path, **open_kwargs) -> MemoryCore:
    """Create a new memory directory at `dest` from a bundle. Run `reindex()` afterwards for vectors."""
    from .core import MemoryCore  # noqa: PLC0415

    src, dest = Path(src), Path(dest)
    manifest = read_manifest(src)
    _require_empty_dir(dest, "destination")
    core = MemoryCore.open(dest, **open_kwargs)
    try:
        entities = [Entity(id=r["id"], kind=r["kind"], name=r["name"], aliases=r.get("aliases", []),
                           meta=r.get("meta", {}), created_at=timeutil.from_iso(r["created_at"]))
                    for r in _read_jsonl(src / "entities.jsonl")]
        episodes = []
        for path in sorted((src / "episodes").glob("*.jsonl")) if (src / "episodes").exists() else []:
            for r in _read_jsonl(path):
                episodes.append(Episode(
                    id=r["id"], kind=r["kind"], source=r["source"], space=r["space"], actor=r.get("actor"),
                    content=r["content"], occurred_at=timeutil.from_iso(r["occurred_at"]),
                    recorded_at=timeutil.from_iso(r["recorded_at"]), meta=r.get("meta", {})))
        memories = []
        for r in _read_jsonl(src / "memories.jsonl"):
            memories.append(Memory(
                id=r["id"], kind=r["kind"], text=r["text"], scope=r["scope"], subjects=r.get("subjects", []),
                importance=r["importance"], strength=r["strength"], status=r["status"],
                valid_from=timeutil.from_iso(r.get("valid_from")), valid_to=timeutil.from_iso(r.get("valid_to")),
                sources=r.get("sources", []), derived_by=r.get("derived_by"), meta=r.get("meta", {}),
                created_at=timeutil.from_iso(r["created_at"]), updated_at=timeutil.from_iso(r["updated_at"]),
                last_recalled_at=timeutil.from_iso(r.get("last_recalled_at"))))

        tokenize = core.tokenizer.tokenize
        with core.store.tx() as c:
            for e in entities:
                core.store.insert_entity(c, e)
            for ep in episodes:
                core.store.insert_episode(c, ep, fts_document(tokenize(ep.content)))
            for m in memories:
                core.store.insert_memory(c, m, fts_document(tokenize(m.text)))
            core.store.set_meta("imported_from", json.dumps(
                {"generator": manifest.get("generator"), "exported_at": manifest.get("exported_at")}), conn=c)

        for sub in ("identity", "attachments"):
            if (src / sub).exists():
                shutil.copytree(src / sub, dest / sub, dirs_exist_ok=True)
    except BaseException:
        core.close()
        raise
    return core
