"""Consolidation: turn new episodes into memories (add / update / skip), mem0-style.

The model proposes; code decides what is allowed. In particular the model can never widen visibility:
memories about people stay inside the space where they were learned (or `owner` for the owner's private
chat). Only memories with no person subjects may become `public`, and only the being's own feelings `self`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from . import timeutil
from .llm import LLM, parse_json
from .models import EntityKind, Episode, EpisodeKind, Memory, MemoryKind
from .scope import OWNER, PUBLIC, SELF, Audience, space_scope

log = logging.getLogger("memory_core.consolidate")

PROMPT_VERSION = "consolidate-v2"
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
- If the new messages tell how a plan, wish or open question in an existing memory turned out (it happened, \
it was cancelled, how it went, a rating), use op "update" on that memory: one sentence with both the plan and \
the outcome. Do not add the outcome as a separate memory.
- If two or more existing memories record the same thing (the same fact or event, or a plan and how it turned \
out), use op "merge" with "targets" (their E-labels) and one text that keeps every detail of them, plus \
anything the new messages add. Before any "update", check the other existing memories: when one of them is \
about the same thing (e.g. the plan and its outcome), use "merge" on all of them instead, so one memory is left.
- subjects: who or what the memory is about. Use P-labels for participants; for other people or things give \
{"name": ..., "type": "person|group|place|work|concept|thing"}.
- evidence: the M-labels of the messages the memory is based on.
- importance: 0.0-1.0 (1.0 = core facts about someone close; 0.2 = minor detail).
- visibility: "space" (default) or "public" (only for general knowledge or slang that is not about any \
person) or "self" (only for my own feelings or preferences).
- Never store secrets such as passwords, keys, phone numbers or ID numbers.

Answer with JSON only:
{"memories": [{"op": "add" | "update" | "merge", "target": "E1 (update only)", "targets": ["E1", "E2"] (merge \
only), "kind": "fact|event|relation|preference|lexicon", "text": "...", "subjects": ["P1", {"name": "...", \
"type": "work"}], "evidence": ["M3"], "importance": 0.5, "visibility": "space"}]}
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
    elif op == "merge":
        olds = [labels_e[t] for t in dict.fromkeys(item.get("targets") or []) if t in labels_e]
        if len(olds) < 2:
            raise ValueError("merge needs at least two existing memories (E-labels)")
        # The merged memory keeps the scope the originals share (core.merge refuses mixed scopes), so a merge can
        # never make anything more visible; the new evidence is added to its sources.
        new = core.merge([m.id for m in olds], text, derived_by=derived_by,
                         importance=importance if "importance" in item else None,
                         sources=[s for s in sources if item.get("evidence")])
        report.updated.extend((m.id, new.id) for m in olds)
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


# ---------------------------------------------------------------- which spaces to consolidate now


def due_spaces(core, *, min_new: int = 10, idle_minutes: float = 60, now=None) -> list[str]:
    """Spaces to consolidate now: those with at least `min_new` new episodes, and also quiet ones (no new
    episode for `idle_minutes`) with at least one, so a short chat does not wait for the next one."""
    now = now or timeutil.now()
    busy = set(pending(core, min_new))
    due = list(busy)
    if idle_minutes and idle_minutes > 0:
        for space in pending(core, 1):
            if space in busy:
                continue
            last = core.recent(space, 1)
            if last and timeutil.age_days(last[-1].occurred_at, now) * 1440 >= idle_minutes:
                due.append(space)
    return due


# ---------------------------------------------------------------- remembering on purpose

REMEMBER_PROMPT_VERSION = "remember-v1"

REMEMBER_SYSTEM = """\
You maintain the long-term memory of an AI companion ("I"). I want to remember something right now. Below is \
the new memory and the existing memories that may be about the same thing.

Decide:
- "add": it is new; give "text" (the same sentence, with relative times such as "today" or "next Saturday" \
turned into dates using Today below).
- "update": it changes, corrects or completes ONE existing memory (e.g. how a plan in it turned out). Give \
"target" and one text that keeps what is still true of the old memory plus the new information.
- "merge": it and two or more existing memories record the same thing. Give "targets" and one text keeping \
every detail.
- "skip": an existing memory already says exactly this.

Write in the language of the memories, one or two self-contained sentences, names instead of pronouns, \
dates instead of relative times.
Answer with JSON only: {"op": "add" | "update" | "merge" | "skip", "target": "E1", "targets": ["E1", "E2"], \
"text": "..."}"""


@dataclass
class RememberResult:
    op: str  # add | update | merge | skip
    memory: Memory  # the memory that now holds it (new, or the existing one for skip)
    replaced: list[str] = field(default_factory=list)  # ids of memories it superseded


def remember(core, llm: LLM | None, memory: Memory, audience: Audience, *, k: int = 8) -> RememberResult:
    """Store a memory on purpose, but first look at related memories in the same scope and let the model decide
    whether it is new, updates one, merges several or is already known. Without a model, or when nothing
    related exists, or when the model's answer is unusable, it is simply added."""
    related = []
    if llm is not None:
        related = [h.memory for h in core.recall(memory.text, audience, k=k, reinforce=False)
                   if h.memory.scope == memory.scope and h.memory.kind != MemoryKind.DIARY]
    if not related:
        return RememberResult("add", core.add_memory(memory))
    labels = {f"E{i + 1}": m for i, m in enumerate(related)}
    today = timeutil.now().astimezone()
    user = "\n".join([f"Today: {today:%Y-%m-%d} ({today:%A})", "", "New memory:", memory.text, "",
                      "Existing memories:",
                      *(f"{k_}: {m.text}" for k_, m in labels.items())])
    derived_by = f"{llm.model_id}/{REMEMBER_PROMPT_VERSION}"
    try:
        data = parse_json(llm.complete(REMEMBER_SYSTEM, user))
        op = data.get("op", "add")
        text = str(data.get("text") or "").strip() or memory.text
        if op == "skip":
            target = labels.get(data.get("target", "")) or related[0]
            return RememberResult("skip", target)
        if op == "update" and data.get("target") in labels:
            old = labels[data["target"]]
            new = core.supersede(old.id, Memory(
                kind=memory.kind, text=text, scope=old.scope, subjects=list(dict.fromkeys(
                    [*old.subjects, *memory.subjects])), sources=list(dict.fromkeys([*old.sources, *memory.sources])),
                importance=max(old.importance, memory.importance), derived_by=derived_by))
            return RememberResult("update", new, [old.id])
        if op == "add" and text != memory.text:
            memory = replace(memory, text=text)
        if op == "merge":
            olds = [labels[t] for t in dict.fromkeys(data.get("targets") or []) if t in labels]
            if len(olds) >= 2:
                new = core.merge([m.id for m in olds], text, derived_by=derived_by, sources=memory.sources,
                                 importance=max([memory.importance, *(m.importance for m in olds)]))
                return RememberResult("merge", new, [m.id for m in olds])
    except Exception as exc:  # noqa: BLE001 - the model being down or confused must never lose the memory
        log.info("remember: checking related memories failed, adding as is: %s", exc)
    return RememberResult("add", core.add_memory(memory))
