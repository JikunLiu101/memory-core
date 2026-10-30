"""SQLite storage. One database file per being. Low-level: no business rules beyond integrity."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import Entity, Episode, Memory
from .timeutil import from_iso, to_iso

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE entity (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    name       TEXT NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE entity_alias (
    alias     TEXT NOT NULL,
    entity_id TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    PRIMARY KEY (alias, entity_id)
);
CREATE TABLE episode (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    space       TEXT NOT NULL,
    actor       TEXT REFERENCES entity(id),
    content     TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    meta        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX episode_space_time ON episode(space, occurred_at);
CREATE INDEX episode_time ON episode(occurred_at);
CREATE TABLE memory (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    text             TEXT NOT NULL,
    scope            TEXT NOT NULL,
    importance       REAL NOT NULL,
    strength         REAL NOT NULL,
    status           TEXT NOT NULL,
    valid_from       TEXT,
    valid_to         TEXT,
    derived_by       TEXT,
    meta             TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_recalled_at TEXT
);
CREATE INDEX memory_scope_status ON memory(scope, status);
CREATE TABLE memory_subject (
    memory_id TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    entity_id TEXT NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    PRIMARY KEY (memory_id, entity_id)
);
CREATE INDEX memory_subject_entity ON memory_subject(entity_id);
CREATE TABLE memory_source (
    memory_id  TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    episode_id TEXT NOT NULL,
    PRIMARY KEY (memory_id, episode_id)
);
CREATE INDEX memory_source_episode ON memory_source(episode_id);
CREATE TABLE embedding (
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    model_id    TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (target_type, target_id, model_id)
);
CREATE VIRTUAL TABLE memory_fts USING fts5(tokens);
CREATE VIRTUAL TABLE episode_fts USING fts5(tokens);
"""


def _dumps(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self._lock = threading.RLock()
        # timeout: wait for other processes (e.g. a chat adapter and an MCP server sharing one memory).
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # Bumped whenever recall-relevant memory data or vectors change (invalidates vector caches).
        self.generation = 0
        self._init_schema()

    # ---------------------------------------------------------------- plumbing

    def _init_schema(self) -> None:
        exists = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if exists:
            version = self.get_meta("schema_version")
            if version != str(SCHEMA_VERSION):
                raise RuntimeError(
                    f"{self.path}: schema version {version}, this memory-core supports {SCHEMA_VERSION}"
                )
            return
        try:
            with self.tx() as c:
                # executescript() would commit implicitly, so run statements one by one inside the transaction.
                for statement in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
                    c.execute(statement)
                c.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise RuntimeError("this Python's SQLite was built without FTS5, which memory-core needs") from exc
            raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get_meta(self, key: str) -> str | None:
        with self.read() as c:
            row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        sql = "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        if conn is not None:
            conn.execute(sql, (key, value))
            return
        with self.tx() as c:
            c.execute(sql, (key, value))

    def delete_meta(self, key: str) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM meta WHERE key = ?", (key,))

    def meta_items(self, prefix: str) -> dict[str, str]:
        with self.read() as c:
            rows = c.execute("SELECT key, value FROM meta WHERE substr(key, 1, ?) = ? ORDER BY key",
                             (len(prefix), prefix))
            return {r[0]: r[1] for r in rows}

    # ---------------------------------------------------------------- entities

    def insert_entity(self, c: sqlite3.Connection, e: Entity) -> None:
        c.execute(
            "INSERT INTO entity(id, kind, name, meta, created_at) VALUES (?, ?, ?, ?, ?)",
            (e.id, e.kind, e.name, _dumps(e.meta), to_iso(e.created_at)),
        )
        for alias in dict.fromkeys(e.aliases):
            c.execute("INSERT OR IGNORE INTO entity_alias(alias, entity_id) VALUES (?, ?)", (alias, e.id))

    def add_alias(self, c: sqlite3.Connection, entity_id: str, alias: str) -> None:
        c.execute("INSERT OR IGNORE INTO entity_alias(alias, entity_id) VALUES (?, ?)", (alias, entity_id))

    def entity_exists(self, c: sqlite3.Connection, entity_id: str) -> bool:
        return c.execute("SELECT 1 FROM entity WHERE id = ?", (entity_id,)).fetchone() is not None

    def resolve_alias(self, c: sqlite3.Connection, alias: str) -> list[str]:
        rows = c.execute("SELECT entity_id FROM entity_alias WHERE alias = ? ORDER BY entity_id", (alias,))
        return [r[0] for r in rows]

    def get_entity(self, entity_id: str) -> Entity | None:
        with self.read() as c:
            row = c.execute("SELECT * FROM entity WHERE id = ?", (entity_id,)).fetchone()
            if row is None:
                return None
            aliases = [r[0] for r in c.execute(
                "SELECT alias FROM entity_alias WHERE entity_id = ? ORDER BY alias", (entity_id,)
            )]
        return self._entity(row, aliases)

    def iter_entities(self) -> Iterator[Entity]:
        with self.read() as c:
            rows = c.execute("SELECT * FROM entity ORDER BY id").fetchall()
            alias_rows = c.execute("SELECT entity_id, alias FROM entity_alias ORDER BY alias").fetchall()
        aliases: dict[str, list[str]] = {}
        for r in alias_rows:
            aliases.setdefault(r[0], []).append(r[1])
        for row in rows:
            yield self._entity(row, aliases.get(row["id"], []))

    def entities_mentioned_in(self, text: str, min_len: int = 2) -> list[str]:
        """Entity ids whose name or an alias appears verbatim in `text`."""
        with self.read() as c:
            rows = c.execute(
                "SELECT entity_id FROM entity_alias WHERE length(alias) >= ?1 AND instr(?2, alias) > 0"
                " UNION SELECT id FROM entity WHERE length(name) >= ?1 AND instr(?2, name) > 0"
                " ORDER BY 1",
                (min_len, text),
            )
            return [r[0] for r in rows]

    @staticmethod
    def _entity(row: sqlite3.Row, aliases: list[str]) -> Entity:
        return Entity(
            id=row["id"], kind=row["kind"], name=row["name"], aliases=aliases,
            meta=json.loads(row["meta"]), created_at=from_iso(row["created_at"]),
        )

    # ---------------------------------------------------------------- episodes

    def insert_episode(self, c: sqlite3.Connection, ep: Episode, fts_doc: str) -> None:
        cur = c.execute(
            "INSERT INTO episode(id, kind, source, space, actor, content, occurred_at, recorded_at, meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ep.id, ep.kind, ep.source, ep.space, ep.actor, ep.content,
             to_iso(ep.occurred_at), to_iso(ep.recorded_at), _dumps(ep.meta)),
        )
        c.execute("INSERT INTO episode_fts(rowid, tokens) VALUES (?, ?)", (cur.lastrowid, fts_doc))

    def episode_exists(self, c: sqlite3.Connection, episode_id: str) -> bool:
        return c.execute("SELECT 1 FROM episode WHERE id = ?", (episode_id,)).fetchone() is not None

    def get_episode(self, episode_id: str) -> Episode | None:
        with self.read() as c:
            row = c.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()
        return self._episode(row) if row else None

    def list_episodes(
        self, space: str | None = None, since: str | None = None, until: str | None = None,
        limit: int | None = None, newest_first: bool = False,
    ) -> list[Episode]:
        where, params = [], []
        if space is not None:
            where.append("space = ?")
            params.append(space)
        if since is not None:
            where.append("occurred_at >= ?")
            params.append(since)
        if until is not None:
            where.append("occurred_at < ?")
            params.append(until)
        sql = "SELECT * FROM episode"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY occurred_at {0}, id {0}".format("DESC" if newest_first else "ASC")
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.read() as c:
            return [self._episode(r) for r in c.execute(sql, params)]

    def episodes_after(self, space: str, after_rowid: int, limit: int) -> list[tuple[int, Episode]]:
        """Episodes of a space in insertion order, after a cursor (rowid)."""
        with self.read() as c:
            rows = c.execute("SELECT rowid AS _rowid, * FROM episode WHERE space = ? AND rowid > ?"
                             " ORDER BY rowid LIMIT ?", (space, after_rowid, limit)).fetchall()
        return [(r["_rowid"], self._episode(r)) for r in rows]

    def space_heads(self) -> dict[str, int]:
        """Latest episode rowid per space."""
        with self.read() as c:
            return {r[0]: r[1] for r in c.execute("SELECT space, MAX(rowid) FROM episode GROUP BY space")}

    def search_episodes(self, match: str, space: str | None, limit: int) -> list[Episode]:
        sql = ("SELECT e.* FROM episode_fts JOIN episode e ON e.rowid = episode_fts.rowid"
               " WHERE episode_fts MATCH ?")
        params: list = [match]
        if space is not None:
            sql += " AND e.space = ?"
            params.append(space)
        sql += " ORDER BY bm25(episode_fts) LIMIT ?"
        params.append(limit)
        with self.read() as c:
            return [self._episode(r) for r in c.execute(sql, params)]

    @staticmethod
    def _episode(row: sqlite3.Row) -> Episode:
        return Episode(
            id=row["id"], kind=row["kind"], source=row["source"], space=row["space"], actor=row["actor"],
            content=row["content"], occurred_at=from_iso(row["occurred_at"]),
            recorded_at=from_iso(row["recorded_at"]), meta=json.loads(row["meta"]),
        )

    # ---------------------------------------------------------------- memories

    def insert_memory(self, c: sqlite3.Connection, m: Memory, fts_doc: str) -> None:
        cur = c.execute(
            "INSERT INTO memory(id, kind, text, scope, importance, strength, status, valid_from, valid_to,"
            " derived_by, meta, created_at, updated_at, last_recalled_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.kind, m.text, m.scope, m.importance, m.strength, m.status,
             to_iso(m.valid_from) if m.valid_from else None, to_iso(m.valid_to) if m.valid_to else None,
             m.derived_by, _dumps(m.meta), to_iso(m.created_at), to_iso(m.updated_at),
             to_iso(m.last_recalled_at) if m.last_recalled_at else None),
        )
        c.execute("INSERT INTO memory_fts(rowid, tokens) VALUES (?, ?)", (cur.lastrowid, fts_doc))
        for entity_id in dict.fromkeys(m.subjects):
            c.execute("INSERT INTO memory_subject(memory_id, entity_id) VALUES (?, ?)", (m.id, entity_id))
        for episode_id in dict.fromkeys(m.sources):
            c.execute("INSERT INTO memory_source(memory_id, episode_id) VALUES (?, ?)", (m.id, episode_id))
        self.generation += 1

    def update_memory(self, c: sqlite3.Connection, memory_id: str, **fields) -> None:
        allowed = {"status", "valid_to", "importance", "strength", "updated_at", "last_recalled_at", "meta"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update fields {sorted(unknown)}")
        if not fields:
            return
        values = [(_dumps(v) if k == "meta" else v) for k, v in fields.items()]
        assignments = ", ".join(f"{k} = ?" for k in fields)
        cur = c.execute(f"UPDATE memory SET {assignments} WHERE id = ?", (*values, memory_id))
        if cur.rowcount == 0:
            raise KeyError(memory_id)
        if fields.keys() & {"status", "valid_to"}:  # only these change what vector search may return
            self.generation += 1

    def get_memories(self, ids: Iterable[str]) -> dict[str, Memory]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return {}
        out: dict[str, Memory] = {}
        with self.read() as c:
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start : chunk_start + 500]
                marks = ",".join("?" * len(chunk))
                rows = c.execute(f"SELECT * FROM memory WHERE id IN ({marks})", chunk).fetchall()
                subjects = self._links(c, "memory_subject", "entity_id", chunk)
                sources = self._links(c, "memory_source", "episode_id", chunk)
                for row in rows:
                    out[row["id"]] = self._memory(row, subjects.get(row["id"], []), sources.get(row["id"], []))
        return out

    @staticmethod
    def _links(c: sqlite3.Connection, table: str, column: str, ids: list[str]) -> dict[str, list[str]]:
        marks = ",".join("?" * len(ids))
        rows = c.execute(
            f"SELECT memory_id, {column} FROM {table} WHERE memory_id IN ({marks}) ORDER BY {column}", ids
        )
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r[0], []).append(r[1])
        return out

    def iter_memory_ids(self) -> list[str]:
        with self.read() as c:
            return [r[0] for r in c.execute("SELECT id FROM memory ORDER BY id")]

    def _recallable_filter(self, scopes: frozenset[str], now_iso: str, kinds: list[str] | None,
                           include_archived: bool) -> tuple[str, list]:
        statuses = ["active", "archived"] if include_archived else ["active"]
        sql = (f" m.status IN ({','.join('?' * len(statuses))})"
               f" AND m.scope IN ({','.join('?' * len(scopes))})"
               " AND (m.valid_to IS NULL OR m.valid_to > ?)")
        params: list = [*statuses, *sorted(scopes), now_iso]
        if kinds:
            sql += f" AND m.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        return sql, params

    def search_memories_text(self, match: str, scopes: frozenset[str], now_iso: str, limit: int,
                             kinds: list[str] | None, include_archived: bool) -> list[str]:
        cond, params = self._recallable_filter(scopes, now_iso, kinds, include_archived)
        sql = ("SELECT m.id FROM memory_fts JOIN memory m ON m.rowid = memory_fts.rowid"
               f" WHERE memory_fts MATCH ? AND {cond} ORDER BY bm25(memory_fts) LIMIT ?")
        with self.read() as c:
            return [r[0] for r in c.execute(sql, [match, *params, limit])]

    def memories_about(self, entity_ids: list[str], scopes: frozenset[str], now_iso: str, limit: int,
                       kinds: list[str] | None, include_archived: bool) -> list[str]:
        if not entity_ids:
            return []
        cond, params = self._recallable_filter(scopes, now_iso, kinds, include_archived)
        marks = ",".join("?" * len(entity_ids))
        sql = ("SELECT m.id, COUNT(*) AS hits FROM memory_subject s JOIN memory m ON m.id = s.memory_id"
               f" WHERE s.entity_id IN ({marks}) AND {cond}"
               " GROUP BY m.id ORDER BY hits DESC, m.importance DESC, m.updated_at DESC LIMIT ?")
        with self.read() as c:
            return [r[0] for r in c.execute(sql, [*entity_ids, *params, limit])]

    def recallable_vector_rows(self, model_id: str, include_archived: bool) -> list[sqlite3.Row]:
        statuses = ("active", "archived") if include_archived else ("active",)
        sql = ("SELECT m.id, m.scope, m.kind, m.valid_to, e.dim, e.vector FROM embedding e"
               " JOIN memory m ON m.id = e.target_id"
               f" WHERE e.target_type = 'memory' AND e.model_id = ? AND m.status IN ({','.join('?' * len(statuses))})"
               " ORDER BY m.id")
        with self.read() as c:
            return c.execute(sql, (model_id, *statuses)).fetchall()

    @staticmethod
    def _memory(row: sqlite3.Row, subjects: list[str], sources: list[str]) -> Memory:
        return Memory(
            id=row["id"], kind=row["kind"], text=row["text"], scope=row["scope"],
            importance=row["importance"], strength=row["strength"], status=row["status"],
            valid_from=from_iso(row["valid_from"]), valid_to=from_iso(row["valid_to"]),
            derived_by=row["derived_by"], meta=json.loads(row["meta"]),
            created_at=from_iso(row["created_at"]), updated_at=from_iso(row["updated_at"]),
            last_recalled_at=from_iso(row["last_recalled_at"]), subjects=subjects, sources=sources,
        )

    def active_memory_rows(self, kinds_excluded: Iterable[str] = ()) -> list[sqlite3.Row]:
        excluded = list(kinds_excluded)
        sql = "SELECT id, kind, scope, importance, strength, created_at, last_recalled_at FROM memory" \
              " WHERE status = 'active'"
        if excluded:
            sql += f" AND kind NOT IN ({','.join('?' * len(excluded))})"
        with self.read() as c:
            return c.execute(sql + " ORDER BY id", excluded).fetchall()

    def find_diary(self, day: str) -> str | None:
        """The active diary memory of a day (fallback when the meta pointer is missing, e.g. after import)."""
        with self.read() as c:
            row = c.execute("SELECT id FROM memory WHERE kind = 'diary' AND status = 'active'"
                            " AND json_extract(meta, '$.date') = ? ORDER BY created_at DESC LIMIT 1",
                            (day,)).fetchone()
        return row[0] if row else None

    def memories_citing(self, episode_ids: Iterable[str]) -> list[str]:
        ids = list(dict.fromkeys(episode_ids))
        out: list[str] = []
        with self.read() as c:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                rows = c.execute(f"SELECT DISTINCT memory_id FROM memory_source WHERE episode_id IN"
                                 f" ({','.join('?' * len(chunk))})", chunk)
                out.extend(r[0] for r in rows)
        return sorted(set(out))

    # ---------------------------------------------------------------- forgetting (the one exception to append-only)

    def tombstone_memory(self, c: sqlite3.Connection, memory_id: str, now_iso: str, meta: dict) -> None:
        """Remove a memory's content everywhere; the row stays as a tombstone (status 'forgotten')."""
        c.execute("DELETE FROM memory_fts WHERE rowid = (SELECT rowid FROM memory WHERE id = ?)", (memory_id,))
        c.execute("DELETE FROM embedding WHERE target_type = 'memory' AND target_id = ?", (memory_id,))
        c.execute("UPDATE memory SET text = '', status = 'forgotten', updated_at = ?, meta = ? WHERE id = ?",
                  (now_iso, _dumps(meta), memory_id))
        self.generation += 1

    def tombstone_episode(self, c: sqlite3.Connection, episode_id: str, now_iso: str) -> None:
        c.execute("DELETE FROM episode_fts WHERE rowid = (SELECT rowid FROM episode WHERE id = ?)", (episode_id,))
        c.execute("UPDATE episode SET content = '', meta = ? WHERE id = ?",
                  (_dumps({"forgotten_at": now_iso}), episode_id))

    # ---------------------------------------------------------------- embeddings

    def get_embeddings(self, model_id: str, ids: Iterable[str]) -> dict[str, bytes]:
        ids = list(ids)
        out: dict[str, bytes] = {}
        with self.read() as c:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                rows = c.execute(f"SELECT target_id, vector FROM embedding WHERE target_type = 'memory'"
                                 f" AND model_id = ? AND target_id IN ({','.join('?' * len(chunk))})",
                                 (model_id, *chunk))
                out.update({r[0]: r[1] for r in rows})
        return out

    def upsert_embedding(self, c: sqlite3.Connection, target_type: str, target_id: str, model_id: str,
                         dim: int, vector: bytes, created_at: str) -> None:
        c.execute(
            "INSERT INTO embedding(target_type, target_id, model_id, dim, vector, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(target_type, target_id, model_id)"
            " DO UPDATE SET dim = excluded.dim, vector = excluded.vector, created_at = excluded.created_at",
            (target_type, target_id, model_id, dim, vector, created_at),
        )
        self.generation += 1

    def memories_missing_embedding(self, model_id: str) -> list[tuple[str, str]]:
        with self.read() as c:
            rows = c.execute(
                "SELECT m.id, m.text FROM memory m WHERE m.status IN ('active', 'archived') AND NOT EXISTS"
                " (SELECT 1 FROM embedding e WHERE e.target_type = 'memory' AND e.target_id = m.id"
                " AND e.model_id = ?) ORDER BY m.id",
                (model_id,),
            )
            return [(r[0], r[1]) for r in rows]

    def embedding_models(self) -> dict[str, int]:
        with self.read() as c:
            rows = c.execute("SELECT model_id, COUNT(*) FROM embedding GROUP BY model_id ORDER BY model_id")
            return {r[0]: r[1] for r in rows}

    # ---------------------------------------------------------------- full-text index maintenance

    def rebuild_fts(self, tokenize) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM memory_fts")
            c.execute("DELETE FROM episode_fts")
            for rowid, text in c.execute("SELECT rowid, text FROM memory").fetchall():
                c.execute("INSERT INTO memory_fts(rowid, tokens) VALUES (?, ?)", (rowid, tokenize(text)))
            for rowid, text in c.execute("SELECT rowid, content FROM episode").fetchall():
                c.execute("INSERT INTO episode_fts(rowid, tokens) VALUES (?, ?)", (rowid, tokenize(text)))

    # ---------------------------------------------------------------- stats

    def counts(self) -> dict[str, int]:
        with self.read() as c:
            return {
                "entities": c.execute("SELECT COUNT(*) FROM entity").fetchone()[0],
                "episodes": c.execute("SELECT COUNT(*) FROM episode").fetchone()[0],
                "memories": c.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
                "embeddings": c.execute("SELECT COUNT(*) FROM embedding").fetchone()[0],
            }
