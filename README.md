# memory-core

**English** | [中文](README.zh-CN.md)

A local-first, framework-agnostic long-term memory core for AI companions.

> Status: **pre-alpha (M1)**. Storage, hybrid recall, the bundle format, LLM consolidation, an MCP server and an
> AstrBot adapter work and are tested. Reflection, decay and hard forgetting are next. APIs may change.

## Why

An AI companion should still be *itself* after you switch chat framework, LLM, or embedding model. Most
memory plugins keep their data in a plugin-specific format, and many treat vectors as the memory itself —
but vectors belong to one embedding model and become useless when the model changes.

memory-core is built on a few rules:

1. **Text is the memory; vectors are only an index.** Raw experiences and readable memories are kept
   forever; vectors can be recomputed with any model at any time (`memory-core reindex`).
2. **Raw experiences are append-only.** Everything else (memories, diaries, profiles, indexes) is derived from
   them and records its sources, so it can be rebuilt, audited, or deleted along the provenance chain.
3. **Framework-agnostic.** A Python library (and soon a local MCP server); chat frameworks connect through
   thin adapters.
4. **Model-agnostic.** Any OpenAI-compatible endpoint for embeddings (Ollama, vLLM, LM Studio, OpenAI...).
5. **Local-first.** One SQLite file plus a folder. No telemetry, no network calls except to the model
   endpoints you configure.
6. **Portable.** A versioned, plain-text, git-friendly export format — the *soul bundle* ([SPEC.md](SPEC.md)).
7. **Privacy by scope.** Every memory has a visibility scope, enforced in code: what happened in one group
   chat is not brought up in another.

## Concepts

| | What | Rebuildable? |
|---|---|---|
| **Episode** | A raw experience: a message received or sent, something observed (a web page, a video), an action | No — the source of truth |
| **Memory** | A derived, one-sentence memory: fact, event, relation, reflection, diary, preference, lexicon (slang), style | Yes, from episodes |
| **Entity** | A person, group, place, work... with aliases (account IDs, nicknames) | — |
| **Identity** | Markdown "core memory" documents: `self.md`, `owner.md`, `now.md` | Hand-edited |
| **Index** | Full-text (SQLite FTS5) and vectors, keyed by model | Yes, always |

**Scopes**: `public` (anywhere), `self` (its own feelings), `owner` (only in private with the owner),
`space:<space>` (only in that place).

## Quick start

```bash
pip install -e ".[zh]"          # zh = jieba for better Chinese full-text search (optional)
memory-core init ./soul
memory-core record ./soul --space web --source browser --kind observation --content "Watched a video about Genshin"
memory-core remember ./soul --scope public --kind fact --text "Genshin Impact is an open-world game"
memory-core recall ./soul "Genshin" --space web
memory-core export ./soul ./soul-bundle      # portable plain-text backup
```

Semantic recall: run an embedding model locally, e.g. `ollama pull bge-m3`, then in `soul/config.toml`:

```toml
[embedding]
provider = "openai-compatible"
base_url = "http://127.0.0.1:11434/v1"
model = "bge-m3"
```

and `memory-core reindex ./soul`.

### Python

```python
from memory_core import MemoryCore, Episode, Memory, Audience

with MemoryCore.open("./soul") as core:
    ep = core.record(Episode(kind="message_in", content="I don't eat coriander", space="chat:group:1",
                             source="qq", actor="qq:1001"))
    core.add_memory(Memory(kind="preference", text="Ming doesn't eat coriander",
                           scope="space:chat:group:1", subjects=["qq:1001"], sources=[ep.id]))
    for hit in core.recall("coriander", Audience(space="chat:group:1")):
        print(hit.score, hit.memory.text)
```

## Consolidation

Episodes become memories through a chat model (any OpenAI-compatible endpoint, or your framework's own model):

```toml
# soul/config.toml
[llm]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
api_key_env = "DEEPSEEK_API_KEY"   # the key stays in an environment variable, not in the file

[owner]
spaces = ["qq:private:<owner id>"] # private chats with the owner: their memories get the "owner" scope
```

```bash
memory-core consolidate ./soul
```

For each place with new episodes, the model sees the new messages and the related memories it already has, and
proposes to **add** a memory, **update** one (the old one is kept, marked `superseded`), or nothing. The model
proposes, code decides: it can never widen visibility — memories about people stay in the place they were learned
(or `owner`), only memories about no person can become `public`. A place's cursor only advances after its batch
was applied, so a failed call loses nothing.

## MCP server

Any MCP client (Claude Code, DeepSeek Harness, Codex CLI, ...) can share the same memory:

```bash
memory-core mcp ./soul             # stdio; add --read-only for clients that should only read
```

```json
{"mcpServers": {"memory": {"command": "memory-core", "args": ["mcp", "/path/to/soul"]}}}
```

Tools: `recall`, `recent`, `profile`, `stats`, `record`, `remember`, `consolidate`. Identity documents are exposed
as resources (`identity://self`, ...). Every tool that returns memories takes the current `space` and only returns
what is visible there.

## Adapters

* **AstrBot** — [`adapters/astrbot`](adapters/astrbot/README.md): records every visible message and the bot's own
  answers, injects recalled memories before each reply, consolidates periodically with AstrBot's own chat model,
  and gives the model `memory_recall` / `memory_remember` tools.

## Roadmap

| | |
|---|---|
| M0 ✅ | Storage, append-only episodes, hybrid recall (full-text + vectors + entities), scopes, soul bundle, tests |
| M1 ✅ | Consolidation (add / update / supersede, entity aliases), MCP server, AstrBot adapter |
| M2 | Reflection (diary, `now.md`), decay and archiving, hard forget along provenance, write gates & proposals |
| M3 | Public benchmarks (LoCoMo, LongMemEval), docs |
| M4 | Experience collectors: browser, video sites |
| M5 | Lexicon (slang/meme) learning, style learning, identity proposals with rollback, inner monologue |

## Acknowledgements

Design ideas from [Generative Agents](https://github.com/joonspk-research/generative_agents) (memory
stream, retrieval scoring, reflection), [mem0](https://github.com/mem0ai/mem0) (add / update / delete
consolidation), [Graphiti](https://github.com/getzep/graphiti) (bi-temporal facts),
[Letta](https://github.com/letta-ai/letta) (core vs archival memory),
[dsh-memory](https://github.com/FuRongJun-1999/dsh-memory) (raw text as source of truth, layered write gates),
and the AstrBot plugins LivingMemory and self_learning (ideas only; no code copied).

## License

[Apache-2.0](LICENSE)
