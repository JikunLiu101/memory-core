"""The owner's dashboard: a plugin view in AstrBot (pages/memory/) and the read-only Web API behind it.

AstrBot serves plugin Web APIs only to a logged-in dashboard user (or an API key with the plugin scope), so these
views show every scope. Nothing here changes the memory: recall is tested without reinforcement, and the
abilities are listed without noticing a change.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from memory_core import Audience, MemoryStatus, browse

PREFIX = "/astrbot_plugin_memory_core/page"
_STATUSES = {s.value for s in MemoryStatus}


def _ok(data: Any) -> dict:
    return {"status": "ok", "data": data}


def _error(message: str) -> dict:
    return {"status": "error", "message": message}


def _strings(value, allowed: set[str] | None = None) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    out = [str(v).strip() for v in (value or []) if str(v).strip()]
    return [v for v in out if allowed is None or v in allowed]


def _int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def list_args(body: dict) -> dict:
    """Arguments for browse.list_memories from a request body."""
    body = body or {}
    return {
        "text": str(body.get("text") or "")[:200],
        "kinds": _strings(body.get("kinds")),
        "statuses": _strings(body.get("statuses"), _STATUSES),
        "scope": str(body.get("scope") or "").strip(),
        "entity": str(body.get("entity") or "").strip(),
        "offset": _int(body.get("offset"), 0, 0, 10**9),
        "limit": _int(body.get("limit"), 50, 1, 200),
    }


def recall_args(body: dict) -> dict:
    """Arguments for browse.explain_recall: the query, who would hear it, how many."""
    body = body or {}
    query = str(body.get("query") or "").strip()[:500]
    if not query:
        raise ValueError("query is empty")
    space = str(body.get("space") or "").strip() or "webchat:private:owner"
    return {
        "query": query,
        "audience": Audience(space, owner_present=bool(body.get("owner_present"))),
        "k": _int(body.get("k"), 8, 1, 50),
        "include_archived": bool(body.get("include_archived")),
    }


def graph_args(query) -> dict:
    statuses = _strings(query.get("statuses"), _STATUSES) or [MemoryStatus.ACTIVE.value]
    return {"statuses": statuses, "scope": str(query.get("scope") or "").strip()}


class Panel:
    """Registers the dashboard's routes on the plugin's context."""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    @property
    def core(self):
        if self.plugin.core is None:
            raise RuntimeError("memory-core is not open yet")
        return self.plugin.core

    def register(self) -> bool:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            return False
        routes = [
            ("overview", self.overview, ["GET"]),
            ("spaces", self.spaces, ["GET"]),
            ("memories", self.memories, ["POST"]),
            ("memory", self.memory, ["GET"]),
            ("recall", self.recall, ["POST"]),
            ("graph", self.graph, ["GET"]),
            ("entity", self.entity, ["GET"]),
        ]
        for name, handler, methods in routes:
            register(f"{PREFIX}/{name}", self._guarded(handler), methods, f"memory-core dashboard: {name}")
        return True

    @staticmethod
    def _guarded(handler):
        async def run(**_):
            try:
                return _ok(await handler())
            except (ValueError, KeyError, LookupError) as exc:
                return _error(str(exc))
            except Exception as exc:  # noqa: BLE001 - the page shows the message instead of a blank screen
                from astrbot.api import logger  # noqa: PLC0415

                logger.warning(f"[memory-core] dashboard {handler.__name__} failed: {exc}")
                return _error(f"{type(exc).__name__}: {exc}")

        run.__name__ = handler.__name__
        return run

    @staticmethod
    def _request():
        from astrbot.api.web import request  # noqa: PLC0415 - only exists inside a Web API handler

        return request

    async def _body(self) -> dict:
        body = self._request().json()
        if inspect.isawaitable(body):
            body = await body
        return body if isinstance(body, dict) else {}

    def _query(self):
        return self._request().query

    # ------------------------------------------------------------------------------------- handlers

    async def overview(self):
        plugin = self.plugin
        data = await asyncio.to_thread(browse.overview, self.core)
        try:
            data["abilities"] = [{"key": a.key, "name": a.name, "text": a.text} for a in plugin._abilities()]
        except Exception as exc:  # noqa: BLE001
            data["abilities_error"] = str(exc)
        if plugin.cfg.get("inner_state", True):
            try:
                from memory_core.now import describe  # noqa: PLC0415

                data["inner_state"] = await asyncio.to_thread(
                    lambda: describe(plugin._snapshot(), owner_present=True, owner_title=plugin.owner_title))
            except Exception as exc:  # noqa: BLE001
                data["inner_state_error"] = str(exc)
        data["owner_title"] = plugin.owner_title
        data["self_name"] = (plugin.cfg.get("self_name") or "").strip()
        data["owner_spaces"] = plugin._owner_spaces()
        return data

    async def spaces(self):
        return {"spaces": await asyncio.to_thread(browse.spaces, self.core),
                "owner_spaces": self.plugin._owner_spaces()}

    async def memories(self):
        args = list_args(await self._body())
        return await asyncio.to_thread(lambda: browse.list_memories(self.core, **args))

    async def memory(self):
        memory_id = str(self._query().get("id") or "").strip()
        if not memory_id:
            raise ValueError("id is missing")
        found = await asyncio.to_thread(browse.memory_detail, self.core, memory_id)
        if found is None:
            raise KeyError(f"no memory {memory_id}")
        return found

    async def recall(self):
        args = recall_args(await self._body())
        return await asyncio.to_thread(lambda: browse.explain_recall(self.core, **args))

    async def graph(self):
        args = graph_args(self._query())
        return await asyncio.to_thread(lambda: browse.graph(self.core, **args))

    async def entity(self):
        entity_id = str(self._query().get("id") or "").strip()
        if not entity_id:
            raise ValueError("id is missing")
        statuses = _strings(self._query().get("statuses"), _STATUSES) or [MemoryStatus.ACTIVE.value]
        found = await asyncio.to_thread(lambda: browse.entity_detail(self.core, entity_id, statuses=statuses))
        if found is None:
            raise KeyError(f"no entity {entity_id}")
        return found
