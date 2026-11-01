# Changelog

## 0.6.0 (2026-11-01)

- On PyPI as **`memory-core-lib`** (`pip install memory-core-lib`; the package is still `import memory_core`).
  The name `memory-core` is too close to an existing project there.
- The AstrBot adapter moved to its own repository,
  [astrbot_plugin_memory_core](https://github.com/JikunLiu101/astrbot_plugin_memory_core) (with its history), so
  AstrBot's plugin market can install it; its tests moved with it.
- `abilities_block(..., marks=False)` and `abilities_news()`: the list without what is new, and what is new as a
  block of its own.
- Prompt caching (AstrBot plugin): the system prompt is laid out by how often each part changes, so the provider
  caches the longest start: identity, voice examples and the memory guide first, then what other plugins put
  there, then the self-image (rewritten daily) and the abilities list, then who is speaking. The abilities list no
  longer marks what is new or just lost (`abilities_block(..., marks=False)`), so it only changes when an ability
  comes or goes, not again when the mark runs out; what is new goes with the user's message in
  `<abilities_news>` (`abilities_news()`), taken out of the history like `<now>`.

## 0.5.0 (2026-10-31)

- `memory_core.now.owner_messages` is public (it was `_owner_messages`): the times the owner said something,
  for other plugins that want to know how long the owner has been away.
- A read-only dashboard (AstrBot plugin view `memory`, AstrBot 4.24.2+): overview, memories with the messages
  they came from and all their versions, recall explained (each part of the score and the rank in each channel;
  two places side by side; nothing is reinforced), and a graph of the entities the memories are about. No
  third-party front-end libraries. The views live in the library (`memory_core.browse`: `overview`,
  `list_memories`, `memory_detail`, `history`, `explain_recall`, `graph`, `entity_detail`, `pending_counts`), so
  other hosts can offer them too; they show every scope and are meant for the owner only.
- `RecallHit.ranks`: the place a memory got in each retrieval channel.

- Prompt caching (AstrBot adapter): the system prompt now holds only what stays the same between replies
  (identity, memory guide, abilities, who is speaking); the inner state `<now>` goes with the user's message, like
  the recalled memories. Before each request, `<long_term_memory>` and `<now>` blocks are taken out of the earlier
  user messages in the history (`strip_transient`), so the history is identical from one request to the next and
  the provider's prompt cache covers it (and old memories no longer pile up in it). The memory guide tells the
  being these blocks are notes from the system, not something the other person said.
- The being can write its diary itself (`diary_write`, AstrBot adapter, only in the owner's private chat,
  `diary_tool_per_day`, default 3). `write_diary(extend=True)` keeps what is written and continues it with what
  happened since (`unwritten()`); `note` is something the being wants to be sure to write about. The daily routine
  finishes such a day early next morning, and the self-image is refreshed once per day (`self_image:day`).
  With `show` (default), the diary is sent to the owner as it is. The abilities list also tells the being to use
  the tool right away when it says it will do something on the list.
- What the being can do (`memory_core.abilities`): the host lists the being's abilities (a stable key, a short
  verb-phrase name, one line on how it works); the list goes into the prompt, and anything not on it is something
  the being cannot do yet. The last known list is kept in the store: a new ability is marked new for a while
  (`fresh_hours`) and the being notices it once, a lost one too. AstrBot adapter: the list is collected from the
  active plugins (a plugin describes itself with `being_abilities(owner)` returning (key, name, text) tuples;
  otherwise its description and its tools are used); in the owner's private chat a change is recorded as the
  being's own thought there, so it reaches its memory and diary. Settings `abilities_enabled`,
  `abilities_fresh_hours`.
- Seeing pictures (`memory_core.vision`, prompt `vision-v1`): a vision model describes each picture plainly (what
  kind of picture, what is in it, its text word for word); the description is cached by the picture's content, so
  the same sticker is looked at once. Viewers are tried in order (a local model first, a remote one as a fallback).
  The picture itself can be kept in `attachments/images/`. Optional dependency: `pillow` (`[vision]`).
  AstrBot adapter: pictures in a message (and in the message it quotes) are looked at when it arrives; the episode
  says `[图片：...]` instead of `[图片]`; a text-only chat model gets the descriptions instead of the pictures.
  Settings `vision_enabled`, `vision_providers`, `vision_keep_pictures` (owner / all / none), `vision_max_images`,
  `vision_timeout`.
- Daily schedule (`memory_core.now.day_schedule`, `jitter`): bedtime and wake-up drift a little each day (give or
  take 20% of an hour, the same all day, so every plugin agrees); asleep means very low energy, and a note when the
  owner wakes her up. AstrBot adapter: `sleep_hour`, `wake_hour`, `timing_jitter`.
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
