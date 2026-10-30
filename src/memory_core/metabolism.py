"""Memory metabolism: what keeps a growing memory healthy.

- `fade`: memories that are not recalled lose strength over time (see `MemoryCore.effective_strength`);
  weak, unimportant ones are archived. Archived memories are not brought up by themselves any more, but a
  deliberate search (`recall(..., include_archived=True)`) still finds them, and finding one makes it active
  again.
- `merge_duplicates`: memories that say the same thing (by vector similarity, confirmed by a model) become one.

Hard forgetting is `MemoryCore.forget`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from . import timeutil
from .llm import LLM, parse_json
from .models import MemoryKind, MemoryStatus

log = logging.getLogger("memory_core.metabolism")

MERGE_PROMPT_VERSION = "merge-v1"
_MERGE_CHECKED = "merge:checked:"

MERGE_SYSTEM = """\
You maintain the long-term memory of an AI companion. Below are memories that look similar. Decide whether \
they record the same thing (the same fact or the same event, possibly worded differently).

If they do, write ONE memory that keeps every detail from all of them (names, dates, numbers), as one \
self-contained sentence or two, in the language of the memories. If they are related but different things, \
or any of them contradict each other, do not merge.

Answer with JSON only: {"same": true, "text": "...", "importance": 0.0-1.0} or {"same": false}"""


@dataclass
class FadeReport:
    checked: int = 0
    archived: list[str] = field(default_factory=list)


@dataclass
class MergeReport:
    groups: int = 0
    merged: list[tuple[list[str], str]] = field(default_factory=list)  # (old ids, new id)
    kept: int = 0
    errors: list[str] = field(default_factory=list)


def fade(core, *, now: datetime | None = None, archive_below: float = 0.25, keep_importance: float = 0.8,
         min_age_days: float = 14, protected_kinds=(MemoryKind.DIARY,)) -> FadeReport:
    """Archive active memories whose strength has faded below `archive_below`. Never archives memories with
    importance >= `keep_importance`, younger than `min_age_days`, or of a protected kind (diaries)."""
    now = now or timeutil.now()
    report = FadeReport()
    rows = core.store.active_memory_rows(kinds_excluded=[str(k) for k in protected_kinds])
    candidates = []
    for row in rows:
        report.checked += 1
        if row["importance"] >= keep_importance:
            continue
        if timeutil.age_days(timeutil.from_iso(row["created_at"]), now) < min_age_days:
            continue
        candidates.append(row["id"])
    if not candidates:
        return report
    now_iso = timeutil.to_iso(now)
    memories = core.store.get_memories(candidates)
    with core.store.tx() as c:
        for memory_id in candidates:
            m = memories[memory_id]
            if core.effective_strength(m, now) < archive_below:
                core.store.update_memory(c, memory_id, status=MemoryStatus.ARCHIVED, updated_at=now_iso)
                report.archived.append(memory_id)
    return report


def similar_groups(core, threshold: float = 0.8, max_group: int = 6,
                   protected_kinds=(MemoryKind.DIARY,)) -> list[list[str]]:
    """Groups of active memories (same scope) whose vectors are at least `threshold` similar."""
    if core.embedder is None:
        return []
    rows = core.store.active_memory_rows(kinds_excluded=[str(k) for k in protected_kinds])
    by_scope: dict[str, list[str]] = {}
    for row in rows:
        by_scope.setdefault(row["scope"], []).append(row["id"])
    groups = []
    for ids in by_scope.values():
        if len(ids) < 2:
            continue
        vectors = core.store.get_embeddings(core.embedder.model_id, ids)
        ids = [i for i in ids if i in vectors]
        if len(ids) < 2:
            continue
        matrix = np.vstack([np.frombuffer(vectors[i], dtype=np.float32) for i in ids])
        sims = matrix @ matrix.T
        parent = list(range(len(ids)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in zip(*np.nonzero(np.triu(sims, k=1) >= threshold)):
            parent[find(int(a))] = find(int(b))
        clusters: dict[int, list[str]] = {}
        for index, memory_id in enumerate(ids):
            clusters.setdefault(find(index), []).append(memory_id)
        groups += [sorted(c)[:max_group] for c in clusters.values() if len(c) > 1]
    return sorted(groups)


def _group_key(ids: list[str]) -> str:
    return _MERGE_CHECKED + hashlib.sha256(",".join(sorted(ids)).encode()).hexdigest()[:24]


def merge_duplicates(core, llm: LLM, *, threshold: float = 0.8, max_groups: int = 20) -> MergeReport:
    """Ask the model about each group of similar memories; merge the ones that say the same thing.
    A group the model kept apart is not asked about again (until its members change)."""
    report = MergeReport()
    for ids in similar_groups(core, threshold)[:max_groups]:
        key = _group_key(ids)
        if core.store.get_meta(key):
            continue
        report.groups += 1
        memories = core.store.get_memories(ids)
        user = "\n".join(f"- {memories[i].text}" for i in ids)
        try:
            data = parse_json(llm.complete(MERGE_SYSTEM, user))
            if data.get("same") and str(data.get("text", "")).strip():
                importance = data.get("importance")
                importance = min(1.0, max(0.0, float(importance))) if importance is not None else None
                new = core.merge(ids, str(data["text"]).strip(), importance=importance,
                                 derived_by=f"{llm.model_id}/{MERGE_PROMPT_VERSION}")
                report.merged.append((ids, new.id))
            else:
                core.store.set_meta(key, "kept")
                report.kept += 1
        except (ValueError, KeyError, TypeError) as exc:
            report.errors.append(f"{exc}")
            log.info("merge of %s failed: %s", ids, exc)
    return report
