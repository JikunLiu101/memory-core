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
| Model tools | `memory_recall(query)`, `memory_remember(text, kind)` |
| Admin commands | `/mem_status`, `/mem_consolidate` (runs now, for every chat with new messages), `/mem_diary` (writes today's diary now) |

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
pip install "memory-core @ git+https://github.com/JikunLiu101/memory-core@v0.2.0"
```

Copy `astrbot_plugin_memory_core/` into AstrBot's `data/plugins/`, restart AstrBot, then set at least
`owner_ids` in the plugin's settings. The memory directory defaults to
`data/plugin_data/astrbot_plugin_memory_core/soul`; set `soul_dir` to keep it somewhere you back up.

Semantic recall needs an embedding model; put it in `<soul_dir>/config.toml` (see the main README), for example a
local Ollama `bge-m3`, then run `memory-core reindex <soul_dir>`. Without it, recall uses full-text search and the
people mentioned.
