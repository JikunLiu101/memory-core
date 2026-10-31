"""Data model. See SPEC.md for the portable (bundle) representation of the same records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class EpisodeKind(StrEnum):
    """What happened. Episodes are raw, append-only experiences."""

    MESSAGE_IN = "message_in"  # someone else said something
    MESSAGE_OUT = "message_out"  # the being said something
    OBSERVATION = "observation"  # something it saw: a web page, a video, an image description
    ACTION = "action"  # something it did
    THOUGHT = "thought"  # inner monologue (reserved; not produced by default)


class MemoryKind(StrEnum):
    """Derived, human-readable memories."""

    FACT = "fact"
    EVENT = "event"
    RELATION = "relation"
    REFLECTION = "reflection"
    DIARY = "diary"
    PREFERENCE = "preference"
    LEXICON = "lexicon"  # slang / memes / in-group jargon
    STYLE = "style"  # expression patterns


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"  # faded; kept, not recalled by default
    SUPERSEDED = "superseded"  # replaced by a newer memory
    FORGOTTEN = "forgotten"  # tombstone; content removed


class EntityKind(StrEnum):
    PERSON = "person"
    GROUP = "group"
    PLACE = "place"
    WORK = "work"  # a game, a show, a song...
    CONCEPT = "concept"
    THING = "thing"


@dataclass
class Entity:
    kind: str
    name: str
    aliases: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    created_at: datetime | None = None


@dataclass
class Episode:
    kind: str
    content: str
    space: str  # where it happened, e.g. "qq:group:<id>", "qq:private:<id>", "web"
    source: str  # channel, e.g. "qq", "webchat", "browser"
    actor: str | None = None  # entity id, or an alias that will be resolved / created
    occurred_at: datetime | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    recorded_at: datetime | None = None


@dataclass
class Memory:
    kind: str
    text: str
    scope: str  # see scope.py
    subjects: list[str] = field(default_factory=list)  # entity ids (aliases accepted on write)
    importance: float = 0.5
    strength: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: str = MemoryStatus.ACTIVE
    sources: list[str] = field(default_factory=list)  # episode ids
    derived_by: str | None = None  # e.g. "deepseek-flash/consolidate-v1", or "manual"
    meta: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_recalled_at: datetime | None = None


@dataclass
class RecallHit:
    memory: Memory
    score: float
    relevance: float
    recency: float
    channels: list[str]  # which retrieval channels found it: "text", "vector", "entity"
    ranks: dict[str, int] = field(default_factory=dict)  # its place in each of those channels (1 = first)
