"""Command line: memory-core <command> ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .bundle import import_bundle, read_manifest
from .core import MemoryCore
from .models import Episode, Memory
from .scope import Audience

CONFIG_TEMPLATE = """\
# memory-core settings for this being. Everything here is optional.

[index]
# Full-text tokenizer: "auto" (jieba if installed, else n-grams), "jieba" or "ngram".
tokenizer = "auto"

# Embedding model for semantic recall. Vectors are an index: change the model, then run `memory-core reindex`.
# [embedding]
# provider = "openai-compatible"
# base_url = "http://127.0.0.1:11434/v1"   # local Ollama
# model = "bge-m3"

# Chat model used by `consolidate` (turning episodes into memories). Keep the key out of this file:
# [llm]
# base_url = "https://api.deepseek.com/v1"
# model = "deepseek-flash"
# api_key_env = "DEEPSEEK_API_KEY"

# Spaces that are private conversations with the owner (their memories get the "owner" scope).
# [owner]
# spaces = ["qq:private:<owner id>"]

# [recall]
# relevance = 1.0
# recency = 0.2
# importance = 0.2
# strength = 0.1
# half_life_days = 30
"""


def _print(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


def cmd_init(args) -> int:
    root = Path(args.path)
    with MemoryCore.open(root, embedder=None):
        pass
    config = root / "config.toml"
    if not config.exists():
        config.write_text(CONFIG_TEMPLATE, encoding="utf-8", newline="\n")
    print(f"initialized {root}")
    return 0


def cmd_record(args) -> int:
    with MemoryCore.open(args.path, create=False) as core:
        ep = core.record(Episode(kind=args.kind, content=args.content, space=args.space, source=args.source,
                                 actor=args.actor))
        print(ep.id)
    return 0


def cmd_remember(args) -> int:
    with MemoryCore.open(args.path, create=False) as core:
        m = core.add_memory(Memory(kind=args.kind, text=args.text, scope=args.scope,
                                   importance=args.importance, subjects=args.subject or [],
                                   derived_by="manual"))
        print(m.id)
    return 0


def cmd_recall(args) -> int:
    with MemoryCore.open(args.path, create=False) as core:
        hits = core.recall(args.query, Audience(space=args.space, owner_present=args.owner), k=args.k,
                           reinforce=not args.no_reinforce)
        _print([{"id": h.memory.id, "score": round(h.score, 4), "relevance": round(h.relevance, 4),
                 "channels": h.channels, "kind": h.memory.kind, "scope": h.memory.scope,
                 "text": h.memory.text} for h in hits])
    return 0


def cmd_export(args) -> int:
    with MemoryCore.open(args.path, create=False, embedder=None) as core:
        manifest = core.export_bundle(args.dest)
    _print(manifest["counts"])
    return 0


def cmd_import(args) -> int:
    core = import_bundle(args.bundle, args.path, embedder=None)
    with core:
        _print(core.stats())
    return 0


def cmd_verify(args) -> int:
    manifest = read_manifest(Path(args.bundle))
    _print({"ok": True, "format_version": manifest["format_version"], "counts": manifest["counts"]})
    return 0


def cmd_reindex(args) -> int:
    with MemoryCore.open(args.path, create=False) as core:
        if core.embedder is None:
            print("no [embedding] configured in config.toml", file=sys.stderr)
            return 2
        print(f"embedded {core.reindex()} memories with {core.embedder.model_id}")
    return 0


def cmd_stats(args) -> int:
    with MemoryCore.open(args.path, create=False, embedder=None) as core:
        _print(core.stats())
    return 0


def cmd_consolidate(args) -> int:
    with MemoryCore.open(args.path, create=False) as core:
        r = core.consolidate()
        _print({"episodes": r.episodes, "added": len(r.added), "updated": len(r.updated),
                "rejected": r.rejected})
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import run  # noqa: PLC0415

    return run(args.path, read_only=args.read_only)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memory-core", description="Local long-term memory for AI companions.")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="create a memory directory")
    s.add_argument("path")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("record", help="append an episode")
    s.add_argument("path")
    s.add_argument("--kind", default="message_in")
    s.add_argument("--space", required=True)
    s.add_argument("--source", default="cli")
    s.add_argument("--actor")
    s.add_argument("--content", required=True)
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("remember", help="add a memory by hand")
    s.add_argument("path")
    s.add_argument("--kind", default="fact")
    s.add_argument("--scope", required=True)
    s.add_argument("--importance", type=float, default=0.5)
    s.add_argument("--subject", action="append")
    s.add_argument("--text", required=True)
    s.set_defaults(func=cmd_remember)

    s = sub.add_parser("recall", help="search memories as a given audience would see them")
    s.add_argument("path")
    s.add_argument("query")
    s.add_argument("--space", required=True)
    s.add_argument("--owner", action="store_true", help="the owner is the listener (private chat)")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--no-reinforce", action="store_true")
    s.set_defaults(func=cmd_recall)

    s = sub.add_parser("export", help="export a portable bundle")
    s.add_argument("path")
    s.add_argument("dest")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("import", help="create a memory directory from a bundle")
    s.add_argument("bundle")
    s.add_argument("path")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("verify", help="check a bundle's integrity")
    s.add_argument("bundle")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("reindex", help="compute missing vectors with the configured embedding model")
    s.add_argument("path")
    s.set_defaults(func=cmd_reindex)

    s = sub.add_parser("consolidate", help="turn pending episodes into memories with the configured [llm]")
    s.add_argument("path")
    s.set_defaults(func=cmd_consolidate)

    s = sub.add_parser("mcp", help="serve this memory over MCP (stdio)")
    s.add_argument("path")
    s.add_argument("--read-only", action="store_true")
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("stats", help="show counts")
    s.add_argument("path")
    s.set_defaults(func=cmd_stats)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (KeyError, LookupError, ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
