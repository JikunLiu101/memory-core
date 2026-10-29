"""Visibility of memories. This is a hard rule enforced in code, never left to the model.

Scopes:
  "public"        may be mentioned anywhere
  "self"          the being's own feelings / preferences; visible to itself everywhere.
                  Never put other people's private facts here.
  "owner"         only in a private conversation with the owner
  "space:<space>" only inside that space (e.g. one group chat)
"""

from __future__ import annotations

from dataclasses import dataclass

PUBLIC = "public"
SELF = "self"
OWNER = "owner"
SPACE_PREFIX = "space:"


def space_scope(space: str) -> str:
    if not space:
        raise ValueError("space must be non-empty")
    return SPACE_PREFIX + space


def validate_scope(scope: str) -> str:
    if scope in (PUBLIC, SELF, OWNER):
        return scope
    if scope.startswith(SPACE_PREFIX) and len(scope) > len(SPACE_PREFIX):
        return scope
    raise ValueError(f"invalid scope {scope!r}; use public / self / owner / space:<space>")


@dataclass(frozen=True)
class Audience:
    """Who will hear what is recalled: the current space, and whether the owner is the (only) listener."""

    space: str
    owner_present: bool = False

    def allowed_scopes(self) -> frozenset[str]:
        scopes = {PUBLIC, SELF, space_scope(self.space)}
        if self.owner_present:
            scopes.add(OWNER)
        return frozenset(scopes)


def visible(scope: str, audience: Audience) -> bool:
    return scope in audience.allowed_scopes()
