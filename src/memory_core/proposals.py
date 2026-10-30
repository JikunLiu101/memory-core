"""Write gate for the owner's layer: the being may *propose* a change to its core identity, never make it.

A proposal is stored until the owner approves or rejects it. Approving appends the change to the identity
document under a dated "added later" section (the previous version is kept in `identity/history/`), so the
owner's own text is never rewritten by the model.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import timeutil
from .ids import new_id

_PREFIX = "proposal:"
TARGETS = ("self", "voice")  # owner-edited documents; self-image is the being's own and needs no proposal


@dataclass
class Proposal:
    id: str
    target: str
    text: str
    reason: str
    status: str  # pending | approved | rejected
    created_at: str
    resolved_at: str | None = None
    note: str | None = None


def propose(core, text: str, reason: str = "", target: str = "self") -> Proposal:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")
    text = text.strip()
    if not text:
        raise ValueError("empty proposal")
    p = Proposal(id=new_id(), target=target, text=text, reason=reason.strip(), status="pending",
                 created_at=timeutil.to_iso(timeutil.now()))
    core.store.set_meta(_PREFIX + p.id, json.dumps(asdict(p), ensure_ascii=False))
    return p


def proposals(core, status: str | None = "pending") -> list[Proposal]:
    out = [Proposal(**json.loads(v)) for v in core.store.meta_items(_PREFIX).values()]
    return [p for p in out if status is None or p.status == status]


def find(core, ref: str) -> Proposal | None:
    """By full id or a unique id suffix (handy in chat commands)."""
    ref = ref.strip()
    matches = [p for p in proposals(core, None) if p.id == ref or (len(ref) >= 4 and p.id.endswith(ref))]
    return matches[0] if len(matches) == 1 else None


def resolve(core, proposal_id: str, approve: bool, *, note: str = "",
            section: str = "后来的补充（经主人同意）") -> Proposal:
    p = find(core, proposal_id)
    if p is None:
        raise KeyError(proposal_id)
    if p.status != "pending":
        raise ValueError(f"proposal already {p.status}")
    now = timeutil.now()
    if approve:
        old = core.identity(p.target) or ""
        if old.strip():
            history = core.root / "identity" / "history"
            history.mkdir(exist_ok=True)
            stamp = now.astimezone().strftime("%Y%m%d-%H%M%S")
            (history / f"{p.target}-{stamp}.md").write_text(old, encoding="utf-8", newline="\n")
        heading = f"## {section}"
        body = old.rstrip("\n")
        if heading not in body:
            body += f"\n\n{heading}\n"
        body += f"\n- {now.astimezone().date().isoformat()}：{p.text}"
        core.set_identity(p.target, body.lstrip("\n") + "\n")
    p.status = "approved" if approve else "rejected"
    p.resolved_at = timeutil.to_iso(now)
    p.note = note or None
    core.store.set_meta(_PREFIX + p.id, json.dumps(asdict(p), ensure_ascii=False))
    return p
