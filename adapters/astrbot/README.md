# memory-core for AstrBot

An [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin (`astrbot_plugin_memory_core`, AstrBot ≥ 4.27) that
gives the bot a long-term memory backed by memory-core.

## What it does

| When | What |
|---|---|
| Any message the bot can see (only chats that pass AstrBot's whitelist reach plugins) | recorded as an episode; commands (`/...`) are skipped. Recording does **not** make the bot reply. |
| The model finished an answer | the answer is recorded (works for streamed replies too) |
| Before each model call | the being's identity documents go first in the system prompt, replacing AstrBot's own persona (see below), followed by a short note on how its memory works; a system-stated line says whether the speaker is the owner. Relevant memories, visible in this chat, are added next to the user's message |
| Every `consolidate_interval_minutes` | chats with at least `consolidate_min_new` new messages are consolidated with AstrBot's own chat model |
| Every day after `diary_hour` | the being writes the previous day's diary in its own voice (an owner-only memory, also saved as `diary/<date>.md`) and rewrites `identity/self-image.md` from its recent diaries (old versions go to `identity/history/`) |
| Once a day, after the diary | memory metabolism: faded memories are archived, duplicates merged (`tidy_enabled`, `archive_below`) |
| Model tools | `memory_recall(query)` (also finds faded memories, which brings them back), `memory_remember(text, kind)`, `identity_propose(text, reason)` (a change to the core identity, waits for the owner; only in the owner's private chat, so nobody else can talk it into one). There is no tool to delete memories |
| Owner commands (only the owner, in a private chat, on a platform in `owner_command_platforms`, default only the local `webchat`; anyone else and every group gets no answer) | `/mem_status`, `/mem_consolidate` (runs now, for every chat with new messages), `/mem_diary` (writes today's diary now), `/mem_tidy` (metabolism now), `/mem_forget <words>` then `/mem_forget_do <numbers>` (hard forget, with the source messages; affected diaries are rewritten), `/mem_proposals`, `/mem_approve <id>`, `/mem_reject <id>` |

### Dashboard

AstrBot 4.24.2 and later show plugin views: open **Plugins → memory-core → Views → memory** in the AstrBot
dashboard. It is read-only:

| Page | What |
|---|---|
| Overview | counts by kind and status, active memories by scope, the spaces with their pending messages, what the being can do (as it is listed in its prompt), its inner state now |
| Memories | filter by words, kind, status, scope or entity; each memory with the messages it came from and all its versions (superseded, merged) |
| Recall | type a sentence, pick where it was said and whether the owner is there: what would be recalled, with every part of each score (relevance, recency, importance, strength) and the place it got in each channel (full text, vectors, entities). Compare two places side by side, e.g. to check that owner-only memories stay out of a group. Nothing is reinforced and archived memories are not woken up |
| Graph | who and what the memories are about: by default only the entities, joined when they share memories (thicker = more); "detailed" also draws every memory. Click an entity to read what the being knows about it |

The page uses no third-party libraries and loads nothing from the internet. Its Web API
(`/astrbot_plugin_memory_core/page/*`) shows every scope, so it relies on AstrBot serving plugin Web APIs only to
a logged-in dashboard user or an API key with the `plugin` scope: do not give such a key to anyone else.

### Identity documents

Markdown files in `<soul_dir>/identity/`, read on every reply (edits apply immediately):

| File | Written by | Content |
|---|---|---|
| `self.md` | the owner | core identity: who it is, relationships, looks, personality, voice, hard rules |
| `self-image.md` | the being (daily) | how it sees itself lately; must not contradict `self.md`, carries no private details of others |
| `voice.md` | the owner, later the being | example lines for tone |

They live with the memory, so the persona moves with it to any framework.

Spaces are named `<platform>:group:<id>` or `<platform>:private:<id>`. Accounts listed in `owner_ids` are one
person (`owner_name`) on every platform; their private chats get the `owner` scope.

## Install

```bash
# in AstrBot's Python environment
pip install "memory-core @ git+https://github.com/JikunLiu101/memory-core@v0.5.0"
```

Copy `astrbot_plugin_memory_core/` into AstrBot's `data/plugins/`, restart AstrBot, then set at least
`owner_ids` in the plugin's settings. The memory directory defaults to
`data/plugin_data/astrbot_plugin_memory_core/soul`; set `soul_dir` to keep it somewhere you back up.

Semantic recall needs an embedding model; put it in `<soul_dir>/config.toml` (see the main README), for example a
local Ollama `bge-m3`, then run `memory-core reindex <soul_dir>`. Without it, recall uses full-text search and the
people mentioned.
