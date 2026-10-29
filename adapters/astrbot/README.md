# memory-core for AstrBot

An [AstrBot](https://github.com/AstrBotDevs/AstrBot) plugin (`astrbot_plugin_memory_core`, AstrBot ≥ 4.27) that
gives the bot a long-term memory backed by memory-core.

## What it does

| When | What |
|---|---|
| Any message the bot can see (only chats that pass AstrBot's whitelist reach plugins) | recorded as an episode; commands (`/...`) are skipped. Recording does **not** make the bot reply. |
| The model finished an answer | the answer is recorded (works for streamed replies too) |
| Before each model call | relevant memories, visible in this chat, are added next to the user's message (the system prompt is left untouched so prompt caching keeps working) |
| Every `consolidate_interval_minutes` | chats with at least `consolidate_min_new` new messages are consolidated with AstrBot's own chat model |
| Model tools | `memory_recall(query)`, `memory_remember(text, kind)` |
| Admin commands | `/mem_status`, `/mem_consolidate` (runs now, for every chat with new messages) |

Spaces are named `<platform>:group:<id>` or `<platform>:private:<id>`. Accounts listed in `owner_ids` are one
person (`owner_name`) on every platform; their private chats get the `owner` scope.

## Install

```bash
# in AstrBot's Python environment
pip install "memory-core @ git+https://github.com/JikunLiu101/memory-core@v0.1.0"
```

Copy `astrbot_plugin_memory_core/` into AstrBot's `data/plugins/`, restart AstrBot, then set at least
`owner_ids` in the plugin's settings. The memory directory defaults to
`data/plugin_data/astrbot_plugin_memory_core/soul`; set `soul_dir` to keep it somewhere you back up.

Semantic recall needs an embedding model; put it in `<soul_dir>/config.toml` (see the main README), for example a
local Ollama `bge-m3`, then run `memory-core reindex <soul_dir>`. Without it, recall uses full-text search and the
people mentioned.
