# Changelog

## 0.1.0 (2026-10-29)

First public preview.

- Storage: append-only episodes, derived memories with provenance, entities with aliases, identity documents.
- Hybrid recall: SQLite FTS5 full-text (jieba or character n-grams), vectors keyed by embedding model, and
  mentioned entities; reciprocal-rank fusion, then relevance / recency / importance / strength scoring.
- Visibility scopes (`public`, `self`, `owner`, `space:<space>`) enforced before ranking.
- Soul bundle format v1 ([SPEC.md](SPEC.md)): plain-text JSONL export / import with SHA-256 manifest.
- Consolidation with any chat model: add / update (supersede) memories; the model cannot widen visibility.
- MCP server over stdio with no dependencies; `--read-only`.
- AstrBot adapter.
- CLI: `init`, `record`, `remember`, `recall`, `consolidate`, `export`, `import`, `verify`, `reindex`, `mcp`, `stats`.
