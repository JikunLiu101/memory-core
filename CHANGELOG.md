# Changelog

## Unreleased

- Time sense and inner state (`memory_core.now`): how long since the owner last talked (and whether a talk starts
  after a long absence), today's visits and outings, yesterday's diary, the being's age; energy by the hour minus
  recent effort, longing for the owner growing with time apart, and a mood the model judges after each quiet
  conversation or outing (stored in the soul, fading back to calm). `describe()` renders it in words; things
  about the owner only for the owner. AstrBot adapter: injected before each reply (`inner_state`), mood checked
  every 10 minutes (`mood_idle_minutes`), shown in `/mem_status`.
- Consolidation (prompt `consolidate-v2`): when new messages tell how a plan in an existing memory turned out,
  the memory is updated instead of a second one being added; a new `merge` op joins existing memories that record
  the same thing (e.g. a plan and its outcome) into one, keeping their shared scope. `MemoryCore.merge` takes
  extra `sources`.
- `remember()` (`remember-v1`): remembering on purpose first looks at related memories in the same scope; the
  model decides add / update / merge / skip and turns relative times into dates. The AstrBot `memory_remember`
  tool uses it (`remember_check_related`).
- `due_spaces()`: a chat with fewer new messages than `min_new` is consolidated once it has been quiet for
  `idle_minutes`. The AstrBot adapter uses it (`consolidate_idle_minutes`, default 60).
- Nightly merge (`merge-v2`): a plan and how it turned out count as the same event.

## 0.4.0 (2026-10-30)

- Public spaces (`[spaces] public` in config.toml, default `["web"]`): what the being saw there on its own becomes
  `public` memories; memories drawn from its own thoughts there stay `owner`. The diary calls it "上网".
- AstrBot adapter: the memory guide states when the diary is written and that memories are tidied afterwards;
  every `/mem_` command needs the owner, in a private chat, on `owner_command_platforms` (default: webchat);
  `identity_propose` only works in the owner's private chat.
- Removed a default AstrBot config (`data/`) that had been committed by mistake; it held no keys or accounts.

## 0.3.0 (2026-10-30)

- Memory metabolism (`memory_core.metabolism`): strength fades without recall (half-life 30 days) and recall
  strengthens it (+0.2); `fade` archives weak, unimportant memories (never diaries); a deliberate recall with
  `include_archived=True` brings an archived memory back. `merge_duplicates` merges memories the model confirms
  say the same thing (`MemoryCore.merge`).
- Hard forgetting: `MemoryCore.forget(ids, with_sources=True)` erases all versions of a memory, its source
  episodes and every memory derived from them (diary mirrors included).
- Identity proposals (`memory_core.proposals`): the being proposes, the owner approves; approved changes are
  appended to the identity document, the previous version is kept.
- Bundles include `diary/`; `diary_for` finds a diary without the meta pointer (e.g. after an import).
- AstrBot adapter: daily metabolism, `/mem_tidy`, `/mem_forget` + `/mem_forget_do`, `/mem_proposals`,
  `/mem_approve`, `/mem_reject`, `identity_propose` tool; `memory_recall` also searches archived memories.

## 0.2.0 (2026-10-29)

- Diary and self-image (`memory_core.reflect`): a first-person diary per day (owner-only `diary` memory, mirrored
  to `diary/<date>.md`; a day can start at a chosen hour), and `identity/self-image.md` rewritten from recent
  diaries with old versions kept in `identity/history/`.
- AstrBot adapter: identity documents (`self.md`, `self-image.md`, `voice.md`) become the persona, replacing
  AstrBot's own; a note explains to the model how its memory works; a system-stated line says whether the speaker
  is the owner; daily diary loop and `/mem_diary`; `owner_title` (what the being calls its owner).

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
