"""MemoryCore: the public facade. One directory = one being.

    <root>/
      soul.db        SQLite database (episodes, memories, entities, indexes)
      config.toml    optional settings (tokenizer, embedding model, recall weights)
      identity/      core-memory documents (self.md, owner.md, now.md, ...)
      attachments/   files referenced by episodes
"""

from __future__ import annotations

import logging
import math
import re
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import numpy as np

from . import timeutil
from .embed import Embedder, embedder_from_config
from .ids import new_id
from .models import Entity, EntityKind, Episode, EpisodeKind, Memory, MemoryKind, MemoryStatus, RecallHit
from .scope import Audience, validate_scope
from .store import Store
from .text import Tokenizer, fts_document, fts_query, get_tokenizer

log = logging.getLogger("memory_core")

_IDENTITY_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RRF_K = 10


@dataclass(frozen=True)
class RecallWeights:
    relevance: float = 1.0
    recency: float = 0.2
    importance: float = 0.2
    strength: float = 0.1
    half_life_days: float = 30.0
    min_vector_similarity: float = 0.25
    reinforce: float = 0.2  # strength gained each time a memory is recalled
    strength_half_life_days: float = 30.0  # unrecalled memories fade: strength halves every this many days


def _check_kind(kind: str, enum: type, what: str) -> str:
    if kind in {k.value for k in enum} or (kind.startswith("x-") and len(kind) > 2):
        return kind
    raise ValueError(f"unknown {what} kind {kind!r} (custom kinds must start with 'x-')")


def _check_unit(value: float, name: str) -> float:
    if not (0.0 <= value <= 1.0) or math.isnan(value):
        raise ValueError(f"{name} must be within [0, 1], got {value}")
    return float(value)


def _normalize(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm > 0 else arr


class MemoryCore:
    def __init__(self, root: Path, store: Store, tokenizer: Tokenizer, embedder: Embedder | None,
                 weights: RecallWeights, config: dict | None = None) -> None:
        self.root = root
        self.store = store
        self.tokenizer = tokenizer
        self.embedder = embedder
        self.weights = weights
        self.config = config or {}
        self._vec_cache: dict[tuple[str, bool], tuple[int, tuple]] = {}

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def open(cls, root: str | Path, *, embedder: Embedder | None | str = "config",
             tokenizer: str | None = None, weights: RecallWeights | None = None,
             create: bool = True) -> MemoryCore:
        """Open (or create) a being's memory directory.

        `embedder="config"` reads [embedding] from config.toml; pass None to disable vectors.
        """
        root = Path(root)
        if not (root / "soul.db").exists() and not create:
            raise FileNotFoundError(f"no memory found at {root}")
        root.mkdir(parents=True, exist_ok=True)
        (root / "identity").mkdir(exist_ok=True)
        (root / "attachments").mkdir(exist_ok=True)
        config = cls._load_config(root)

        if embedder == "config":
            embedder = embedder_from_config(config.get("embedding", {}))
        tok = get_tokenizer(tokenizer or config.get("index", {}).get("tokenizer", "auto"))
        if weights is None:
            weights = RecallWeights(**{k: float(v) for k, v in config.get("recall", {}).items()})

        store = Store(root / "soul.db")
        core = cls(root, store, tok, embedder, weights, config)
        if store.get_meta("tokenizer") != tok.id:
            log.info("full-text index built with %s, rebuilding with %s", store.get_meta("tokenizer"), tok.id)
            store.rebuild_fts(lambda text: fts_document(tok.tokenize(text)))
            store.set_meta("tokenizer", tok.id)
        if store.get_meta("created_at") is None:
            store.set_meta("created_at", timeutil.to_iso(timeutil.now()))
        return core

    @staticmethod
    def _load_config(root: Path) -> dict:
        path = root / "config.toml"
        if not path.exists():
            return {}
        with path.open("rb") as fh:
            return tomllib.load(fh)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> MemoryCore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _doc(self, text: str) -> str:
        return fts_document(self.tokenizer.tokenize(text))

    # ---------------------------------------------------------------- entities

    def ensure_entity(self, alias: str, *, kind: str = EntityKind.PERSON, name: str | None = None,
                      aliases: tuple[str, ...] = (), meta: dict | None = None) -> Entity:
        """Return the entity known by `alias`, creating it if needed. Extra `aliases` are attached."""
        if not alias:
            raise ValueError("alias must be non-empty")
        with self.store.tx() as c:
            found = self.store.resolve_alias(c, alias)
            if len(found) > 1:
                raise LookupError(f"alias {alias!r} is ambiguous: {found}")
            if found:
                entity_id = found[0]
                for extra in aliases:
                    self.store.add_alias(c, entity_id, extra)
            else:
                _check_kind(kind, EntityKind, "entity")
                entity = Entity(kind=kind, name=name or alias, aliases=[alias, *aliases], meta=meta or {},
                                id=new_id(), created_at=timeutil.now())
                self.store.insert_entity(c, entity)
                entity_id = entity.id
        return self.store.get_entity(entity_id)

    def add_alias(self, entity_id: str, alias: str) -> None:
        with self.store.tx() as c:
            if not self.store.entity_exists(c, entity_id):
                raise KeyError(entity_id)
            self.store.add_alias(c, entity_id, alias)

    def rename_entity(self, entity_id: str, name: str) -> None:
        if not name.strip():
            raise ValueError("name must be non-empty")
        with self.store.tx() as c:
            if c.execute("UPDATE entity SET name = ? WHERE id = ?", (name.strip(), entity_id)).rowcount == 0:
                raise KeyError(entity_id)

    def entity(self, id_or_alias: str) -> Entity | None:
        found = self.store.get_entity(id_or_alias)
        if found:
            return found
        with self.store.read() as c:
            ids = self.store.resolve_alias(c, id_or_alias)
            if not ids:  # fall back to a unique display name
                ids = [r[0] for r in c.execute("SELECT id FROM entity WHERE name = ?", (id_or_alias,))]
        return self.store.get_entity(ids[0]) if len(ids) == 1 else None

    def _resolve_entity(self, c, ref: str, *, create_as: str | None) -> str:
        if self.store.entity_exists(c, ref):
            return ref
        found = self.store.resolve_alias(c, ref)
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise LookupError(f"alias {ref!r} is ambiguous: {found}")
        if create_as is None:
            raise KeyError(f"unknown entity {ref!r}; create it with ensure_entity() first")
        entity = Entity(kind=create_as, name=ref, aliases=[ref], id=new_id(), created_at=timeutil.now())
        self.store.insert_entity(c, entity)
        return entity.id

    # ---------------------------------------------------------------- episodes (append-only)

    def record(self, episode: Episode) -> Episode:
        """Append a raw experience. Unknown actor aliases become new person entities."""
        _check_kind(episode.kind, EpisodeKind, "episode")
        if not episode.space or not episode.source:
            raise ValueError("episode.space and episode.source are required")
        if not isinstance(episode.content, str):
            raise TypeError("episode.content must be a string")
        now = timeutil.now()
        ep = replace(episode, id=episode.id or new_id(), recorded_at=now,
                     occurred_at=episode.occurred_at or now, meta=dict(episode.meta))
        with self.store.tx() as c:
            if ep.actor:
                ep.actor = self._resolve_entity(c, ep.actor, create_as=EntityKind.PERSON)
            self.store.insert_episode(c, ep, self._doc(ep.content))
        return ep

    def episode(self, episode_id: str) -> Episode | None:
        return self.store.get_episode(episode_id)

    def episodes(self, space: str | None = None, *, since: datetime | None = None,
                 until: datetime | None = None, limit: int | None = None,
                 newest_first: bool = False) -> list[Episode]:
        return self.store.list_episodes(
            space,
            timeutil.to_iso(since) if since else None,
            timeutil.to_iso(until) if until else None,
            limit, newest_first,
        )

    def recent(self, space: str, limit: int = 20) -> list[Episode]:
        """The latest episodes in a space, oldest first (ready to put into a prompt)."""
        return list(reversed(self.episodes(space, limit=limit, newest_first=True)))

    def search_episodes(self, query: str, space: str | None = None, limit: int = 20) -> list[Episode]:
        match = fts_query(self.tokenizer.tokenize(query))
        return self.store.search_episodes(match, space, limit) if match else []

    # ---------------------------------------------------------------- memories

    def add_memory(self, memory: Memory) -> Memory:
        """Store a derived memory. Subjects may be entity ids or unambiguous aliases; sources must exist."""
        _check_kind(memory.kind, MemoryKind, "memory")
        validate_scope(memory.scope)
        if not memory.text.strip():
            raise ValueError("memory.text must be non-empty")
        _check_unit(memory.importance, "importance")
        _check_unit(memory.strength, "strength")
        if memory.status not in {s.value for s in MemoryStatus}:
            raise ValueError(f"unknown status {memory.status!r}")
        now = timeutil.now()
        m = replace(memory, id=memory.id or new_id(), created_at=memory.created_at or now,
                    updated_at=memory.updated_at or now, meta=dict(memory.meta))
        with self.store.tx() as c:
            m.subjects = [self._resolve_entity(c, s, create_as=None) for s in memory.subjects]
            for episode_id in memory.sources:
                if not self.store.episode_exists(c, episode_id):
                    raise KeyError(f"source episode {episode_id!r} does not exist")
            self.store.insert_memory(c, m, self._doc(m.text))
        self._embed_memories([(m.id, m.text)])
        return m

    def memory(self, memory_id: str) -> Memory | None:
        return self.store.get_memories([memory_id]).get(memory_id)

    def supersede(self, old_id: str, new: Memory) -> Memory:
        """Replace a memory with a newer version. The old one stays, marked superseded (with valid_to)."""
        old = self.memory(old_id)
        if old is None:
            raise KeyError(old_id)
        created = self.add_memory(replace(new, meta={**new.meta, "supersedes": old_id}))
        now_iso = timeutil.to_iso(timeutil.now())
        with self.store.tx() as c:
            self.store.update_memory(c, old_id, status=MemoryStatus.SUPERSEDED, valid_to=now_iso,
                                     updated_at=now_iso, meta={**old.meta, "superseded_by": created.id})
        return created

    def set_status(self, memory_id: str, status: str) -> None:
        if status not in {s.value for s in MemoryStatus} or status == MemoryStatus.FORGOTTEN:
            raise ValueError(f"invalid status {status!r} (use forget() to forget)")
        with self.store.tx() as c:
            self.store.update_memory(c, memory_id, status=status,
                                     updated_at=timeutil.to_iso(timeutil.now()))

    def effective_strength(self, m: Memory, now: datetime | None = None) -> float:
        """Stored strength is the strength at the last recall (or creation); it fades from there."""
        since = m.last_recalled_at or m.created_at
        days = timeutil.age_days(since, now or timeutil.now()) if since else 0.0
        return m.strength * 0.5 ** (max(0.0, days) / self.weights.strength_half_life_days)

    def merge(self, memory_ids: list[str], text: str, *, importance: float | None = None,
              derived_by: str | None = None) -> Memory:
        """Replace several memories that say the same thing with one. They must share one scope;
        the originals stay, marked superseded."""
        found = self.store.get_memories(memory_ids)
        olds = [found[i] for i in dict.fromkeys(memory_ids) if i in found]
        if len(olds) < 2:
            raise ValueError("merge needs at least two existing memories")
        if len({m.scope for m in olds}) != 1:
            raise ValueError("cannot merge memories of different scopes")
        if any(m.status != MemoryStatus.ACTIVE for m in olds):
            raise ValueError("only active memories can be merged")
        first = max(olds, key=lambda m: (m.importance, m.created_at))
        starts = [m.valid_from for m in olds if m.valid_from]
        merged = self.add_memory(Memory(
            kind=first.kind, text=text, scope=first.scope,
            subjects=list(dict.fromkeys(s for m in olds for s in m.subjects)),
            sources=list(dict.fromkeys(s for m in olds for s in m.sources)),
            importance=importance if importance is not None else max(m.importance for m in olds),
            valid_from=min(starts) if starts else None, derived_by=derived_by or "merge",
            meta={"merged_from": [m.id for m in olds]},
        ))
        now_iso = timeutil.to_iso(timeutil.now())
        with self.store.tx() as c:
            for m in olds:
                self.store.update_memory(c, m.id, status=MemoryStatus.SUPERSEDED, valid_to=now_iso,
                                         updated_at=now_iso, meta={**m.meta, "merged_into": merged.id})
        return merged

    def forget(self, memory_ids: list[str], *, with_sources: bool = False) -> dict:
        """Hard forget: every version of each memory (supersede / merge chains) loses its content.

        With `with_sources`, the episodes it came from are erased too, and so is every other memory derived
        from those episodes (e.g. that day's diary). Returns the ids and diary dates that were forgotten.
        """
        chain = self._version_chain(memory_ids)
        episodes: set[str] = set()
        if with_sources:
            for m in self.store.get_memories(chain).values():
                episodes.update(m.sources)
            chain = self._version_chain([*chain, *self.store.memories_citing(episodes)])
        memories = self.store.get_memories(chain)
        now_iso = timeutil.to_iso(timeutil.now())
        diaries = sorted({m.meta["date"] for m in memories.values()
                          if m.kind == MemoryKind.DIARY and m.meta.get("date")})
        keep_keys = ("supersedes", "superseded_by", "merged_from", "merged_into", "date")
        with self.store.tx() as c:
            for m in memories.values():
                if m.status != MemoryStatus.FORGOTTEN:
                    keep = {k: v for k, v in m.meta.items() if k in keep_keys}
                    self.store.tombstone_memory(c, m.id, now_iso, {**keep, "forgotten_at": now_iso})
            for episode_id in sorted(episodes):
                self.store.tombstone_episode(c, episode_id, now_iso)
        for day in diaries:
            if self.store.get_meta("diary:" + day) in memories:
                self.store.delete_meta("diary:" + day)
            mirror = self.root / "diary" / f"{day}.md"
            if mirror.exists():
                mirror.unlink()
        return {"memories": sorted(memories), "episodes": sorted(episodes), "diaries": diaries}

    def _version_chain(self, memory_ids) -> list[str]:
        seen: set[str] = set()
        todo = list(memory_ids)
        while todo:
            batch = [i for i in dict.fromkeys(todo) if i not in seen]
            todo = []
            for m in self.store.get_memories(batch).values():
                seen.add(m.id)
                for key in ("supersedes", "superseded_by", "merged_into"):
                    if m.meta.get(key):
                        todo.append(m.meta[key])
                todo.extend(m.meta.get("merged_from") or [])
        return sorted(seen)

    # ---------------------------------------------------------------- recall

    def recall(self, query: str, audience: Audience, k: int = 8, *, kinds: list[str] | None = None,
               include_archived: bool = False, reinforce: bool = True,
               now: datetime | None = None) -> list[RecallHit]:
        """Hybrid recall: full-text + vectors + mentioned entities, fused, then scored.

        Only memories visible to `audience` are ever considered (see scope.py).
        """
        w = self.weights
        now_dt = now or timeutil.now()
        now_iso = timeutil.to_iso(now_dt)
        scopes = audience.allowed_scopes()
        pool = max(k * 4, 20)

        channels: dict[str, list[str]] = {}
        match = fts_query(self.tokenizer.tokenize(query))
        if match:
            channels["text"] = self.store.search_memories_text(match, scopes, now_iso, pool, kinds,
                                                               include_archived)
        if self.embedder is not None:
            try:
                qvec = self.embedder.embed([query])[0]
                channels["vector"] = self._vector_search(qvec, scopes, now_iso, pool, kinds, include_archived)
            except Exception as exc:  # the memory must keep working when the embedding server is down
                log.warning("vector recall skipped: %s", exc)
        mentioned = self.store.entities_mentioned_in(query)
        if mentioned:
            channels["entity"] = self.store.memories_about(mentioned, scopes, now_iso, pool, kinds,
                                                           include_archived)

        active = [name for name, ids in channels.items() if ids]
        if not active:
            return []
        fused: dict[str, float] = {}
        found_by: dict[str, list[str]] = {}
        for name in active:
            for rank, memory_id in enumerate(channels[name]):
                fused[memory_id] = fused.get(memory_id, 0.0) + 1.0 / (_RRF_K + 1 + rank)
                found_by.setdefault(memory_id, []).append(name)
        best_possible = len(active) / (_RRF_K + 1)

        memories = self.store.get_memories(fused)
        hits = []
        for memory_id, raw in fused.items():
            m = memories.get(memory_id)
            if m is None or m.scope not in scopes:  # defence in depth
                continue
            relevance = raw / best_possible
            recency = 0.5 ** (timeutil.age_days(m.valid_from or m.created_at, now_dt) / w.half_life_days)
            m.strength = self.effective_strength(m, now_dt)
            score = (w.relevance * relevance + w.recency * recency
                     + w.importance * m.importance + w.strength * m.strength)
            hits.append(RecallHit(memory=m, score=score, relevance=relevance, recency=recency,
                                  channels=found_by[memory_id]))
        hits.sort(key=lambda h: (-h.score, h.memory.id))
        hits = hits[:k]

        if reinforce and hits:
            with self.store.tx() as c:
                for hit in hits:
                    hit.memory.strength = min(1.0, hit.memory.strength + w.reinforce)
                    hit.memory.last_recalled_at = now_dt
                    fields = {"strength": hit.memory.strength, "last_recalled_at": now_iso}
                    if hit.memory.status == MemoryStatus.ARCHIVED:  # remembered again
                        hit.memory.status = MemoryStatus.ACTIVE
                        fields.update(status=MemoryStatus.ACTIVE, updated_at=now_iso)
                    self.store.update_memory(c, hit.memory.id, **fields)
        return hits

    def about(self, entity_ref: str, audience: Audience, k: int = 20) -> list[Memory]:
        """What the being knows about an entity, as visible to `audience` (most important first)."""
        ent = self.entity(entity_ref)
        if ent is None:
            return []
        ids = self.store.memories_about([ent.id], audience.allowed_scopes(),
                                        timeutil.to_iso(timeutil.now()), k, None, False)
        found = self.store.get_memories(ids)
        return [found[i] for i in ids if i in found]

    def _vectors(self, model_id: str, include_archived: bool) -> tuple:
        key = (model_id, include_archived)
        cached = self._vec_cache.get(key)
        if cached and cached[0] == self.store.generation:
            return cached[1]
        rows = self.store.recallable_vector_rows(model_id, include_archived)
        dims = {r["dim"] for r in rows}
        if len(dims) > 1:
            log.warning("vectors of %s have mixed dimensions %s; run reindex", model_id, sorted(dims))
        dim = max(dims, key=lambda d: sum(1 for r in rows if r["dim"] == d)) if rows else 0
        rows = [r for r in rows if r["dim"] == dim]
        data = (
            [r["id"] for r in rows],
            [r["scope"] for r in rows],
            [r["kind"] for r in rows],
            [r["valid_to"] for r in rows],
            np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
            if rows else np.zeros((0, 0), dtype=np.float32),
        )
        self._vec_cache[key] = (self.store.generation, data)
        return data

    def _vector_search(self, qvec, scopes, now_iso, limit, kinds, include_archived) -> list[str]:
        ids, row_scopes, row_kinds, row_valid_to, matrix = self._vectors(self.embedder.model_id,
                                                                         include_archived)
        if not ids:
            return []
        q = _normalize(qvec)
        if q.shape[0] != matrix.shape[1]:
            log.warning("query vector has %d dims, index has %d; run reindex", q.shape[0], matrix.shape[1])
            return []
        kind_set = set(kinds) if kinds else None
        mask = np.fromiter(
            (s in scopes and (vt is None or vt > now_iso) and (kind_set is None or kd in kind_set)
             for s, kd, vt in zip(row_scopes, row_kinds, row_valid_to)),
            dtype=bool, count=len(ids),
        )
        sims = matrix @ q
        sims[~mask] = -np.inf
        order = np.argsort(-sims, kind="stable")[:limit]
        return [ids[i] for i in order if sims[i] >= self.weights.min_vector_similarity]

    # ---------------------------------------------------------------- embeddings

    def _embed_memories(self, items: list[tuple[str, str]], embedder: Embedder | None = None) -> int:
        embedder = embedder or self.embedder
        if embedder is None or not items:
            return 0
        try:
            vectors = embedder.embed([text for _, text in items])
        except Exception as exc:
            log.warning("embedding failed (%s); run reindex later", exc)
            return 0
        created = timeutil.to_iso(timeutil.now())
        with self.store.tx() as c:
            for (memory_id, _), vec in zip(items, vectors):
                arr = _normalize(vec)
                self.store.upsert_embedding(c, "memory", memory_id, embedder.model_id, int(arr.shape[0]),
                                            arr.tobytes(), created)
        return len(items)

    def reindex(self, embedder: Embedder | None = None, batch_size: int = 64) -> int:
        """Compute vectors for every memory that has none under the given (or configured) model."""
        embedder = embedder or self.embedder
        if embedder is None:
            raise RuntimeError("no embedder configured")
        missing = self.store.memories_missing_embedding(embedder.model_id)
        done = 0
        for start in range(0, len(missing), batch_size):
            chunk = missing[start : start + batch_size]
            vectors = embedder.embed([text for _, text in chunk])  # errors propagate: reindex is explicit
            created = timeutil.to_iso(timeutil.now())
            with self.store.tx() as c:
                for (memory_id, _), vec in zip(chunk, vectors):
                    arr = _normalize(vec)
                    self.store.upsert_embedding(c, "memory", memory_id, embedder.model_id,
                                                int(arr.shape[0]), arr.tobytes(), created)
            done += len(chunk)
        return done

    # ---------------------------------------------------------------- identity (core memory)

    def _identity_path(self, name: str) -> Path:
        if not _IDENTITY_NAME.match(name):
            raise ValueError(f"invalid identity document name {name!r}")
        return self.root / "identity" / f"{name}.md"

    def identity(self, name: str) -> str | None:
        path = self._identity_path(name)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def identity_names(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "identity").glob("*.md") if _IDENTITY_NAME.match(p.stem))

    def set_identity(self, name: str, text: str) -> None:
        path = self._identity_path(name)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(path)

    # ---------------------------------------------------------------- consolidation

    def consolidate(self, llm=None, *, spaces=None, owner_spaces=None, **kwargs):
        """Turn pending episodes into memories. `llm` defaults to [llm] in config.toml."""
        from .consolidate import consolidate  # noqa: PLC0415
        from .llm import llm_from_config  # noqa: PLC0415

        llm = llm or llm_from_config(self.config.get("llm", {}))
        if llm is None:
            raise RuntimeError("no LLM configured (pass llm= or add [llm] to config.toml)")
        if owner_spaces is None:
            owner_spaces = self.config.get("owner", {}).get("spaces", [])
        return consolidate(self, llm, spaces=spaces, owner_spaces=owner_spaces, **kwargs)

    def pending_consolidation(self, min_new: int = 1) -> dict[str, int]:
        from .consolidate import pending  # noqa: PLC0415

        return pending(self, min_new)

    # ---------------------------------------------------------------- export / info

    def export_bundle(self, dest: str | Path) -> dict:
        from .bundle import export_bundle  # noqa: PLC0415

        return export_bundle(self, Path(dest))

    def stats(self) -> dict:
        return {
            **self.store.counts(),
            "embedding_models": self.store.embedding_models(),
            "tokenizer": self.tokenizer.id,
            "embedder": self.embedder.model_id if self.embedder else None,
            "identity": self.identity_names(),
        }
