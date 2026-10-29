"""Timestamps are stored as UTC ISO-8601 strings with millisecond precision, so they sort as text."""

from __future__ import annotations

from datetime import datetime, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("timezone-aware datetime required (got a naive datetime)")
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def from_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return dt


def age_days(then: datetime, ref: datetime) -> float:
    return max(0.0, (ref - then).total_seconds() / 86400.0)
