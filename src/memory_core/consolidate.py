"""Consolidation: turn new episodes into memories (add / update / skip), mem0-style.

The model proposes; code decides what is allowed. In particular the model can never widen visibility:
memories about people stay inside the space where they were learned (or `owner` for the owner's private
chat). Only memories with no person subjects may become `public`, and only the being's own feelings `self`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import timeutil
from .llm import LLM, parse_json
from .models import EntityKind, Episode, EpisodeKind, Memory, MemoryKind
from .scope import OWNER, PUBLIC, SELF, Audience, space_scope

log = logging.getLogger("memory_core.consolidate")

PROMPT_VERSION = "consolidate-v1"
SELF_ALIAS = "self"
_CURSOR = "cursor:consolidate:"
_ALLOWED_KINDS = {MemoryKind.FACT, MemoryKind.EVENT, MemoryKind.RELATION, MemoryKind.PREFERENCE,
                  MemoryKind.LEXICON}
_ENTITY_KINDS = {k.value for k in EntityKind}

SYSTEM_PROMPT = """\
You maintain the long-term memory of an AI companion ("I"). You read a new excerpt of what happened in one \
place and decide which durable memories to keep.

Keep: facts about people (preferences, relationships, plans, what they do), notable events, slang or memes and \
their meaning, and my own preferences or feelings. Skip greetings, small talk with no lasting value, and \
anything already covered by an existing memory with the same meaning.

Rules:
- One memory = one self-contained sentence, written in the language of the conversation. Use names, not \
"he"/"she". Include the date for events when it matters.
- If a new fact changes or contradicts an existing memory (E-labels), use op "update" with that target \
instead of adding a duplicate.
- subjects: who or what the memory is about. Use P-labels for participants; for other people or things give \
{"name": ..., "type": "person|group|place|work|concept|thing"}.
- evidence: the M-labels of the messages the memory is based on.
- importance: 0.0-1.0 (1.0 = core facts about someone close; 0.2 = minor detail).
- visibility: "space" (default) or "public" (only for general knowledge or slang that is not about any \
person) or "self" (only for my own feelings or preferences).
- Never store secrets such as passwords, keys, phone numbers or ID numbers.

Answer with JSON only:
{"memories": [{"op": "add" | "update", "target": "E1 (update only)", "kind": "fact|event|relation|preference|lexicon", \
"text": "...", "subjects": ["P1", {"name": "...", "type": "work"}], "evidence": ["M3"], "importance": 0.5, \
"visibility": "space"}]}
Return {"memories": []} when nothing is worth keeping."""


@dataclass
class ConsolidationReport:
    episodes: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[tuple[str, str]] = field(default_factory=list)  # (old id, new id)
    rejected: list[str] = field(default_factory=list)  # reasons
    spaces: list[str] = field(default_factory=list)

    def merge(self, other: ConsolidationReport) -> None:
        self.episodes += other.episodes
        self.added += other.added
        self.updated += other.updated
        self.rejected += other.rejected
        self.spaces += other.spaces


def pending(core, min_new: int = 1) -> dict[str, int]:
    """Spaces with at least `min_new` episodes not yet consolidated -> count."""
    out = {}
    for space, head in core.store.space_heads().items():
        cursor = int(core.store.get_meta(_CURSOR + space) or 0)
        if head > cursor:
            n = len(core.store.episodes_after(space, cursor, min_new))
            if n >= min_new:
                out[space] = n
    return out


def consolidate(core, llm: LLM, *, spaces: Iterable[str] | None = None, owner_spaces: Iterable[str] = (),
                public_spaces: Iterable[str] = (), batch_size: int = 40, max_batches: int = 10,
                min_new: int = 1) -> ConsolidationReport:
    """Consolidate pending episodes. The cursor of a space only advances after its batch was applied.

    `public_spaces` are places where nothing private happens (e.g. "web", what the being read online on its
    own): what it saw there becomes `public` memories it can bring up anywhere; memories drawn from its own
    thoughts there are `owner` (shared with the owner only)."""
    owner_spaces = set(owner_spaces)
    public_spaces = set(public_spaces) - owner_spaces
    targets = list(spaces) if spaces is not None else list(pending(core, min_new))
    report = ConsolidationReport()
    for space in targets:
        for _ in range(max_batches):
            cursor = int(core.store.get_meta(_CURSOR + space) or 0)
            batch = core.store.episodes_after(space, cursor, batch_size)
            if len(batch) < min_new or not batch:
                break
            part = _consolidate_batch(core, llm, space, [ep for _, ep in batch], space in owner_spaces,
                                      space in public_spaces)
            core.store.set_meta(_CURSOR + space, str(batch[-1][0]))
            report.merge(part)
            if len(batch) < batch_size:
                break
    return report


def _consolidate_batch(core, llm: LLM, space: str, episodes: list[Episode],
                       owner_private: bool, public: bool = False) -> ConsolidationReport:
    me = core.ensure_entity(SELF_ALIAS, name="我")
    labels_p: dict[str, str] = {}  # P-label -> entity id
    entity_label: dict[str, str] = {}
    roster_lines = []
    for ep in episodes:
        if ep.actor and ep.actor not in entity_label and ep.actor != me.id:
            label = f"P{len(labels_p) + 1}"
            labels_p[label] = ep.actor
            entity_label[ep.actor] = label
            ent = core.store.get_entity(ep.actor)
            roster_lines.append(f"{label}: {ent.name if ent else ep.actor}")

    labels_m: dict[str, str] = {}
    transcript = []
    for ep in episodes:
        label = f"M{len(labels_m) + 1}"
        labels_m[label] = ep.id
        when = ep.occurred_at.astimezone().strftime("%Y-%m-%d %H:%M")
        if ep.kind == EpisodeKind.MESSAGE_OUT or ep.actor == me.id:
            who = "我"
        elif ep.actor:
            who = entity_label.get(ep.actor, "?")
        else:
            who = "-"
        kind = "" if ep.kind in (EpisodeKind.MESSAGE_IN, EpisodeKind.MESSAGE_OUT) else f" ({ep.kind})"
        transcript.append(f"{label} [{when}] {who}{kind}: {ep.content}")

    audience = Audience(space, owner_present=owner_private)
    # Participants' names make the entity channel bring up what we already know about them.
    names = [line.split(": ", 1)[1] for line in roster_lines]
    query = (" ".join(names) + " " + " ".join(ep.content for ep in episodes))[-2000:]
    existing = core.recall(query, audience, k=15, reinforce=False)
    labels_e = {f"E{i + 1}": hit.memory for i, hit in enumerate(existing)}

    user = "\n".join([
        f"Place: {space}{' (private chat with my owner)' if owner_private else ''}"
        f"{' (public: what I saw or did on my own, not a conversation)' if public else ''}",
        "Participants:", *(roster_lines or ["(none besides me)"]),
        "", "Existing memories:", *([f"{k}: {m.text}" for k, m in labels_e.items()] or ["(none)"]),
        "", "New messages:", *transcript,
    ])
    data = parse_json(llm.complete(SYSTEM_PROMPT, user))
    items = data.get("memories", [])
    if not isinstance(items, list):
        raise ValueError("'memories' must be a list")

    report = ConsolidationReport(episodes=len(episodes), spaces=[space])
    base_scope = OWNER if owner_private else PUBLIC if public else space_scope(space)
    # In a public place, what the being saw is public, but what it thought there is its own inner life
    # (it may well be about its owner): memories drawn from its own thoughts stay with the owner.
    own = {ep.id for ep in episodes if ep.kind in (EpisodeKind.THOUGHT, EpisodeKind.MESSAGE_OUT) or ep.actor == me.id}
    derived_by = f"{llm.model_id}/{PROMPT_VERSION}"
    for item in items:
        try:
            _apply(core, item, labels_p, labels_m, labels_e, me.id, base_scope, derived_by, report,
                   own if public else set())
        except (ValueError, KeyError, TypeError, LookupError) as exc:
            report.rejected.append(f"{exc}: {str(item)[:200]}")
            log.info("rejected consolidation item: %s", exc)
    return report


def _apply(core, item: dict, labels_p, labels_m, labels_e, self_id: str, base_scope: str, derived_by: str,
           report: ConsolidationReport, owner_only_sources: set[str] = frozenset()) -> None:
    if not isinstance(item, dict):
        raise TypeError("item is not an object")
    op = item.get("op", "add")
    kind = item.get("kind", MemoryKind.FACT)
    if kind not in _ALLOWED_KINDS:
        raise ValueError(f"kind {kind!r} not allowed here")
    text = str(item.get("text", "")).strip()
    if not text:
        raise ValueError("empty text")
    importance = min(1.0, max(0.0, float(item.get("importance", 0.5))))

    subjects: list[str] = []
    for subj in item.get("subjects", []) or []:
        if isinstance(subj, str) and subj in labels_p:
            subjects.append(labels_p[subj])
        elif isinstance(subj, str) and subj in ("我", "I", "me", "self"):
            subjects.append(self_id)
        elif isinstance(subj, dict) and subj.get("name"):
            etype = subj.get("type", EntityKind.CONCEPT)
            etype = etype if etype in _ENTITY_KINDS else EntityKind.CONCEPT
            subjects.append(core.ensure_entity(str(subj["name"]), kind=etype).id)
        elif isinstance(subj, str) and subj.strip():
            subjects.append(core.ensure_entity(subj.strip(), kind=EntityKind.CONCEPT).id)
    subjects = list(dict.fromkeys(subjects))

    sources = [labels_m[e] for e in item.get("evidence", []) or [] if e in labels_m]
    if not sources:
        sources = list(labels_m.values())

    scope = _decide_scope(core, item.get("visibility", "space"), subjects, self_id, base_scope)
    if owner_only_sources.intersection(sources):
        scope = OWNER
    memory = Memory(kind=kind, text=text, scope=scope, subjects=subjects, importance=importance,
                    sources=sources, derived_by=derived_by,
                    valid_from=timeutil.now() if kind == MemoryKind.FACT else None)

    if op == "update":
        target = labels_e.get(item.get("target", ""))
        if target is None:
            raise KeyError(f"unknown update target {item.get('target')!r}")
        # An update may not move a memory to a wider audience than the one it already had.
        if target.scope != memory.scope and not _scope_within(memory.scope, target.scope):
            memory.scope = target.scope
        new = core.supersede(target.id, memory)
        report.updated.append((target.id, new.id))
    elif op == "add":
        report.added.append(core.add_memory(memory).id)
    else:
        raise ValueError(f"unknown op {op!r}")


def _decide_scope(core, requested: str, subjects: list[str], self_id: str, base_scope: str) -> str:
    def is_person(entity_id: str) -> bool:
        ent = core.store.get_entity(entity_id)
        return ent is not None and ent.kind in (EntityKind.PERSON, EntityKind.GROUP) and entity_id != self_id

    if requested == "public" and not any(is_person(s) for s in subjects):
        return PUBLIC
    if requested == "self" and all(s == self_id for s in subjects):
        return SELF
    return base_scope


def _scope_within(candidate: str, original: str) -> bool:
    """True if `candidate` is at most as visible as `original`."""
    if candidate == original:
        return True
    return original == PUBLIC  # narrowing a public memory is fine; anything else keeps the original
