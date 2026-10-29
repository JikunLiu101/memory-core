# Soul bundle format, version 1

A *soul bundle* is the portable form of one being's memory. It is plain UTF-8 text so it can be read without
memory-core, diffed, and kept in git. Implementations MUST be able to import every older format version.

```
<bundle>/
├── manifest.json
├── entities.jsonl
├── episodes/YYYY-MM.jsonl     # one file per month of occurred_at (UTC)
├── memories.jsonl
├── identity/*.md              # core-memory documents
└── attachments/...            # files referenced by episodes (meta paths are relative to this folder)
```

* JSON Lines: one JSON object per line, `\n` line endings, keys sorted, non-ASCII written as-is.
* Timestamps: ISO-8601 with UTC offset and millisecond precision, e.g. `2026-10-29T08:30:00.000+00:00`.
* IDs: 26-character ULIDs (time-sortable). Records are sorted by `occurred_at`, then `id` (episodes) or by `id`.
* **Vectors are not part of the bundle.** They belong to one embedding model; rebuild them after import.

## manifest.json

```json
{
  "format": "memory-core-bundle",
  "format_version": 1,
  "generator": "memory-core 0.1.0",
  "exported_at": "2026-10-29T08:30:00.000+00:00",
  "counts": {"entities": 1, "episodes": 2, "memories": 1, "identity": 1},
  "files": {"memories.jsonl": "<sha256 hex>", "...": "..."}
}
```

`files` lists every other file with its SHA-256. Importers MUST reject a bundle whose files are missing or do
not match, and MUST reject a `format_version` newer than they support.

## entities.jsonl

| field | type | |
|---|---|---|
| `id` | string | |
| `kind` | string | `person`, `group`, `place`, `work`, `concept`, `thing` |
| `name` | string | display name |
| `aliases` | string[] | account IDs, nicknames; how adapters refer to the entity |
| `meta` | object | free-form |
| `created_at` | timestamp | |

## episodes/*.jsonl

Raw, append-only experiences.

| field | type | |
|---|---|---|
| `id` | string | |
| `kind` | string | `message_in`, `message_out`, `observation`, `action`, `thought`, or `x-<custom>` |
| `source` | string | channel, e.g. `qq`, `webchat`, `browser` |
| `space` | string | where it happened, e.g. `qq:group:<id>`, `qq:private:<id>`, `web` |
| `actor` | string \| null | entity id |
| `content` | string | text |
| `occurred_at` | timestamp | when it happened |
| `recorded_at` | timestamp | when it was written down |
| `meta` | object | message ids, reply-to, URL, attachment paths, ... |

## memories.jsonl

| field | type | |
|---|---|---|
| `id` | string | |
| `kind` | string | `fact`, `event`, `relation`, `reflection`, `diary`, `preference`, `lexicon`, `style`, or `x-<custom>` |
| `text` | string | one natural-language memory |
| `scope` | string | `public`, `self`, `owner`, or `space:<space>` |
| `subjects` | string[] | entity ids |
| `sources` | string[] | episode ids this memory was derived from |
| `importance` | number | 0–1 |
| `strength` | number | 0–1; grows when recalled, decays over time |
| `status` | string | `active`, `archived`, `superseded`, `forgotten` |
| `valid_from`, `valid_to` | timestamp \| null | validity of a fact; `valid_to` is set when it is superseded |
| `derived_by` | string \| null | model and prompt version that produced it, or `manual` |
| `meta` | object | e.g. `supersedes` / `superseded_by` |
| `created_at`, `updated_at`, `last_recalled_at` | timestamp (\| null) | |

## Visibility rule

A memory may be shown to an audience in space `S` only if its scope is `public`, `self`, `space:S`, or
`owner` when the owner is the listener in a private conversation. Implementations MUST enforce this before
ranking.
