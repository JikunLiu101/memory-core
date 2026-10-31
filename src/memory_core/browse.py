"""Read-only views of a memory, for the owner's tools (a dashboard, a CLI).

Nothing here changes the memory: no reinforcement, no status change, no notice of abilities. These views show
every scope; they are for the owner, so the host must only offer them to the owner (the AstrBot adapter serves
them behind the dashboard login). `explain_recall` is the exception that shows one audience's view on purpose.
"""

from __future__ import annotations

from datetime import datetime

from . import timeutil
from .models import Episode, Memory, MemoryStatus
from .scope import SPACE_PREFIX, Audience

_META_KEYS = ("supersedes", "superseded_by", "merged_from", "merged_into", "date", "forgotten_at")
_LIMIT_MAX = 200


def _iso(dt: datetime | None) -> str | None:
    return timeutil.to_iso(dt) if dt else None


def _names(core, entity_ids) -> dict[str, dict]:
    out = {}
    for entity_id in dict.fromkeys(entity_ids):
        e = core.store.get_entity(entity_id)
        if e is not None:
            out[entity_id] = {"id": e.id, "name": e.name, "kind": e.kind}
    return out


def memory_view(core, m: Memory, now: datetime | None = None, names: dict[str, dict] | None = None) -> dict:
    """One memory as plain data (JSON-ready)."""
    names = names if names is not None else _names(core, m.subjects)
    return {
        "id": m.id,
        "kind": m.kind,
        "text": m.text,
        "scope": m.scope,
        "status": m.status,
        "importance": round(m.importance, 3),
        "strength": round(m.strength, 3),
        "effective_strength": round(core.effective_strength(m, now), 3),
        "valid_from": _iso(m.valid_from),
        "valid_to": _iso(m.valid_to),
        "derived_by": m.derived_by,
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
        "last_recalled_at": _iso(m.last_recalled_at),
        "subjects": [names[s] for s in m.subjects if s in names],
        "source_count": len(m.sources),
        "meta": {k: m.meta[k] for k in _META_KEYS if k in m.meta},
    }


def episode_view(ep: Episode, names: dict[str, dict]) -> dict:
    actor = names.get(ep.actor or "")
    return {
        "id": ep.id,
        "kind": ep.kind,
        "space": ep.space,
        "source": ep.source,
        "actor": actor["name"] if actor else ep.actor,
        "content": ep.content,
        "occurred_at": _iso(ep.occurred_at),
        "forgotten": "forgotten_at" in ep.meta,
    }


# ------------------------------------------------------------------------------------------- overview


def overview(core) -> dict:
    """Counts by kind and status, entities by kind, the spaces, what is waiting to be consolidated."""
    with core.store.read() as c:
        memories = [{"kind": r[0], "status": r[1], "count": r[2]} for r in c.execute(
            "SELECT kind, status, COUNT(*) FROM memory GROUP BY kind, status ORDER BY kind, status")]
        entities = {r[0]: r[1] for r in c.execute("SELECT kind, COUNT(*) FROM entity GROUP BY kind ORDER BY kind")}
        spaces = [{"space": r[0], "episodes": r[1], "last": r[2]} for r in c.execute(
            "SELECT space, COUNT(*), MAX(occurred_at) FROM episode GROUP BY space ORDER BY MAX(occurred_at) DESC")]
        scopes = {r[0]: r[1] for r in c.execute(
            "SELECT scope, COUNT(*) FROM memory WHERE status = 'active' GROUP BY scope ORDER BY scope")}
    stats = core.stats()
    return {
        "counts": {k: stats[k] for k in ("episodes", "memories", "entities", "embeddings")},
        "memories": memories,
        "entities": entities,
        "active_by_scope": scopes,
        "spaces": spaces,
        "embedder": stats["embedder"],
        "embedding_models": stats["embedding_models"],
        "identity": stats["identity"],
        "pending": pending_counts(core),
    }


def pending_counts(core) -> dict[str, int]:
    """Episodes per space that consolidation has not read yet (the real numbers, not just "some")."""
    from .consolidate import _CURSOR  # noqa: PLC0415

    out = {}
    with core.store.read() as c:
        for space, head in c.execute("SELECT space, MAX(rowid) FROM episode GROUP BY space ORDER BY space").fetchall():
            row = c.execute("SELECT value FROM meta WHERE key = ?", (_CURSOR + space,)).fetchone()
            cursor = int(row[0]) if row else 0
            if head > cursor:
                out[space] = c.execute("SELECT COUNT(*) FROM episode WHERE space = ? AND rowid > ?",
                                       (space, cursor)).fetchone()[0]
    return out


def spaces(core) -> list[str]:
    """Every space the being has been in, newest first."""
    with core.store.read() as c:
        return [r[0] for r in c.execute("SELECT space FROM episode GROUP BY space ORDER BY MAX(occurred_at) DESC")]


# ------------------------------------------------------------------------------------------- memories


def list_memories(core, *, text: str = "", kinds=(), statuses=(), scope: str = "", entity: str = "",
                  offset: int = 0, limit: int = 50) -> dict:
    """Memories, newest change first. `scope` is an exact scope, or "space:*" for any space's.
    `statuses` defaults to everything except forgotten tombstones. `text` matches a substring."""
    where, params = [], []
    statuses = list(statuses) or [s.value for s in MemoryStatus if s != MemoryStatus.FORGOTTEN]
    where.append(f"m.status IN ({','.join('?' * len(statuses))})")
    params += statuses
    if kinds:
        kinds = list(kinds)
        where.append(f"m.kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    if scope == SPACE_PREFIX + "*":
        where.append("substr(m.scope, 1, ?) = ?")
        params += [len(SPACE_PREFIX), SPACE_PREFIX]
    elif scope:
        where.append("m.scope = ?")
        params.append(scope)
    if text.strip():
        where.append("instr(m.text, ?) > 0")
        params.append(text.strip())
    if entity:
        where.append("EXISTS (SELECT 1 FROM memory_subject s WHERE s.memory_id = m.id AND s.entity_id = ?)")
        params.append(entity)
    cond = " AND ".join(where)
    limit = max(1, min(int(limit), _LIMIT_MAX))
    offset = max(0, int(offset))
    with core.store.read() as c:
        total = c.execute(f"SELECT COUNT(*) FROM memory m WHERE {cond}", params).fetchone()[0]
        ids = [r[0] for r in c.execute(f"SELECT m.id FROM memory m WHERE {cond}"
                                       " ORDER BY m.updated_at DESC, m.id LIMIT ? OFFSET ?",
                                       [*params, limit, offset])]
    found = core.store.get_memories(ids)
    now = timeutil.now()
    names = _names(core, (s for m in found.values() for s in m.subjects))
    return {"total": total, "offset": offset, "limit": limit,
            "items": [memory_view(core, found[i], now, names) for i in ids if i in found]}


def history(core, memory_id: str) -> list[Memory]:
    """Every version of a memory (supersede and merge chains), oldest first."""
    chain = core._version_chain([memory_id])
    found = core.store.get_memories(chain)
    return sorted(found.values(), key=lambda m: (m.created_at or datetime.min, m.id))


def memory_detail(core, memory_id: str) -> dict | None:
    """A memory with where it came from (the episodes) and all its versions."""
    m = core.memory(memory_id)
    if m is None:
        return None
    versions = history(core, memory_id)
    episodes = [ep for ep in (core.episode(i) for i in m.sources) if ep is not None]
    episodes.sort(key=lambda ep: (ep.occurred_at or datetime.min, ep.id))
    names = _names(core, [*(s for v in versions for s in v.subjects), *(ep.actor for ep in episodes if ep.actor)])
    now = timeutil.now()
    return {
        "memory": memory_view(core, m, now, names),
        "sources": [episode_view(ep, names) for ep in episodes],
        "history": [memory_view(core, v, now, names) for v in versions],
    }


# ------------------------------------------------------------------------------------------- recall


def explain_recall(core, query: str, audience: Audience, k: int = 8, *, include_archived: bool = False) -> dict:
    """What the being would recall for `query` heard by `audience`, with every part of each score.
    It is a look, not a recall: nothing is reinforced and archived memories are not woken up."""
    w = core.weights
    hits = core.recall(query, audience, k=max(1, min(int(k), 50)), include_archived=include_archived,
                       reinforce=False)
    now = timeutil.now()
    names = _names(core, (s for h in hits for s in h.memory.subjects))
    out = []
    for h in hits:
        view = memory_view(core, h.memory, now, names)
        out.append({
            "memory": view,
            "score": round(h.score, 4),
            "parts": {
                "relevance": round(w.relevance * h.relevance, 4),
                "recency": round(w.recency * h.recency, 4),
                "importance": round(w.importance * h.memory.importance, 4),
                "strength": round(w.strength * view["effective_strength"], 4),
            },
            "channels": {name: h.ranks.get(name) for name in h.channels},
        })
    return {
        "query": query,
        "audience": {"space": audience.space, "owner_present": audience.owner_present},
        "scopes": sorted(audience.allowed_scopes()),
        "mentioned": [n["name"] for n in _names(core, core.store.entities_mentioned_in(query)).values()],
        "vectors": core.embedder is not None,
        "weights": {"relevance": w.relevance, "recency": w.recency, "importance": w.importance,
                    "strength": w.strength},
        "hits": out,
    }


# ------------------------------------------------------------------------------------------- graph


def graph(core, *, statuses=(MemoryStatus.ACTIVE,), scope: str = "", text_chars: int = 60) -> dict:
    """Entities and the memories about them. `links` join a memory to an entity it is about; `pairs` join two
    entities that share memories (weight = how many). Entities about no listed memory are left out."""
    statuses = [str(s) for s in statuses] or [MemoryStatus.ACTIVE.value]
    where = [f"m.status IN ({','.join('?' * len(statuses))})"]
    params: list = list(statuses)
    if scope == SPACE_PREFIX + "*":
        where.append("substr(m.scope, 1, ?) = ?")
        params += [len(SPACE_PREFIX), SPACE_PREFIX]
    elif scope:
        where.append("m.scope = ?")
        params.append(scope)
    cond = " AND ".join(where)
    with core.store.read() as c:
        rows = c.execute(f"SELECT m.id, m.kind, m.text, m.scope, m.status, m.importance FROM memory m"
                         f" WHERE {cond} ORDER BY m.id", params).fetchall()
        ids = [r["id"] for r in rows]
        links: list[tuple[str, str]] = []
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            links += [(r[0], r[1]) for r in c.execute(
                f"SELECT memory_id, entity_id FROM memory_subject WHERE memory_id IN ({','.join('?' * len(chunk))})"
                " ORDER BY memory_id, entity_id", chunk)]
    about: dict[str, list[str]] = {}
    for memory_id, entity_id in links:
        about.setdefault(memory_id, []).append(entity_id)
    counts: dict[str, int] = {}
    for _, entity_id in links:
        counts[entity_id] = counts.get(entity_id, 0) + 1
    pairs: dict[tuple[str, str], int] = {}
    for subjects in about.values():
        subjects = sorted(set(subjects))
        for i, a in enumerate(subjects):
            for b in subjects[i + 1 :]:
                pairs[(a, b)] = pairs.get((a, b), 0) + 1
    names = _names(core, counts)
    return {
        "entities": [{**names[e], "count": n} for e, n in sorted(counts.items()) if e in names],
        "memories": [{"id": r["id"], "kind": r["kind"], "scope": r["scope"], "status": r["status"],
                      "importance": round(r["importance"], 3),
                      "text": r["text"] if len(r["text"]) <= text_chars else r["text"][:text_chars] + "…"}
                     for r in rows if r["id"] in about],
        "links": [{"memory": m, "entity": e} for m, e in links if e in names],
        "pairs": [{"a": a, "b": b, "weight": n} for (a, b), n in sorted(pairs.items())],
    }


def entity_detail(core, entity_id: str, *, statuses=(MemoryStatus.ACTIVE,), limit: int = 100) -> dict | None:
    """An entity, its aliases, and the memories about it (most important first)."""
    e = core.store.get_entity(entity_id)
    if e is None:
        return None
    statuses = [str(s) for s in statuses] or [MemoryStatus.ACTIVE.value]
    with core.store.read() as c:
        ids = [r[0] for r in c.execute(
            "SELECT m.id FROM memory_subject s JOIN memory m ON m.id = s.memory_id"
            f" WHERE s.entity_id = ? AND m.status IN ({','.join('?' * len(statuses))})"
            " ORDER BY m.importance DESC, m.updated_at DESC LIMIT ?",
            [entity_id, *statuses, max(1, min(int(limit), _LIMIT_MAX))])]
    found = core.store.get_memories(ids)
    now = timeutil.now()
    names = _names(core, (s for m in found.values() for s in m.subjects))
    return {
        "entity": {"id": e.id, "name": e.name, "kind": e.kind, "aliases": e.aliases,
                   "created_at": _iso(e.created_at)},
        "memories": [memory_view(core, found[i], now, names) for i in ids if i in found],
    }
