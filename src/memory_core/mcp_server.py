"""Minimal MCP server over stdio (newline-delimited JSON-RPC 2.0), with no third-party dependencies.

Run:  memory-core mcp ./soul [--read-only]
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from typing import IO, Any

from . import __version__
from .core import MemoryCore
from .models import Episode, Memory
from .scope import OWNER, Audience, space_scope

log = logging.getLogger("memory_core.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_AUDIENCE_PROPS = {
    "space": {"type": "string", "description": "Where the conversation happens, e.g. 'qq:group:123', 'web'."},
    "owner_present": {"type": "boolean", "default": False,
                      "description": "True only in a private conversation with the owner."},
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "recall",
        "description": "Search long-term memory. Only memories visible in the given space are returned.",
        "inputSchema": {"type": "object", "required": ["query", "space"], "properties": {
            "query": {"type": "string"}, **_AUDIENCE_PROPS,
            "k": {"type": "integer", "default": 8, "minimum": 1, "maximum": 50}}},
        "write": False,
    },
    {
        "name": "recent",
        "description": "The latest raw episodes (messages, observations, actions) in a space, oldest first.",
        "inputSchema": {"type": "object", "required": ["space"], "properties": {
            "space": {"type": "string"}, "limit": {"type": "integer", "default": 20, "maximum": 200}}},
        "write": False,
    },
    {
        "name": "profile",
        "description": "What is known about a person or thing (by name or alias), as visible in the space.",
        "inputSchema": {"type": "object", "required": ["entity", "space"], "properties": {
            "entity": {"type": "string"}, **_AUDIENCE_PROPS}},
        "write": False,
    },
    {
        "name": "stats",
        "description": "Counts of episodes, memories, entities and vectors.",
        "inputSchema": {"type": "object", "properties": {}},
        "write": False,
    },
    {
        "name": "record",
        "description": "Append a raw experience (append-only).",
        "inputSchema": {"type": "object", "required": ["content", "space", "source"], "properties": {
            "content": {"type": "string"}, "space": {"type": "string"}, "source": {"type": "string"},
            "kind": {"type": "string", "default": "observation",
                     "enum": ["message_in", "message_out", "observation", "action"]},
            "actor": {"type": "string", "description": "Entity id or alias of who said/did it."}}},
        "write": True,
    },
    {
        "name": "remember",
        "description": "Store a memory explicitly. Defaults to being visible only in the given space.",
        "inputSchema": {"type": "object", "required": ["text", "space"], "properties": {
            "text": {"type": "string"}, **_AUDIENCE_PROPS,
            "kind": {"type": "string", "default": "fact",
                     "enum": ["fact", "event", "relation", "preference", "lexicon", "reflection", "diary"]},
            "visibility": {"type": "string", "default": "space", "enum": ["space", "public", "self"]},
            "importance": {"type": "number", "default": 0.5, "minimum": 0, "maximum": 1}}},
        "write": True,
    },
    {
        "name": "consolidate",
        "description": "Turn pending episodes into memories using the configured [llm].",
        "inputSchema": {"type": "object", "properties": {}},
        "write": True,
    },
]


def _audience(args: dict) -> Audience:
    return Audience(space=str(args["space"]), owner_present=bool(args.get("owner_present", False)))


def _memory_view(m: Memory) -> dict:
    return {"id": m.id, "kind": m.kind, "text": m.text, "scope": m.scope, "importance": m.importance,
            "created_at": m.created_at.isoformat() if m.created_at else None}


class Server:
    def __init__(self, core: MemoryCore, *, read_only: bool = False) -> None:
        self.core = core
        self.read_only = read_only

    # ---------------------------------------------------------------- tools

    def call_tool(self, name: str, args: dict) -> Any:
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None:
            raise KeyError(f"unknown tool {name!r}")
        if spec["write"] and self.read_only:
            raise PermissionError("this memory server is read-only")
        core = self.core
        if name == "recall":
            hits = core.recall(args["query"], _audience(args), k=int(args.get("k", 8)))
            return [{**_memory_view(h.memory), "score": round(h.score, 4)} for h in hits]
        if name == "recent":
            return [{"id": e.id, "kind": e.kind, "actor": e.actor, "content": e.content,
                     "occurred_at": e.occurred_at.isoformat()}
                    for e in core.recent(args["space"], int(args.get("limit", 20)))]
        if name == "profile":
            ent = core.entity(args["entity"])
            if ent is None:
                return {"found": False}
            return {"found": True, "name": ent.name, "kind": ent.kind,
                    "memories": [_memory_view(m) for m in core.about(ent.id, _audience(args))]}
        if name == "stats":
            return core.stats()
        if name == "record":
            ep = core.record(Episode(kind=args.get("kind", "observation"), content=args["content"],
                                     space=args["space"], source=args["source"], actor=args.get("actor")))
            return {"id": ep.id}
        if name == "remember":
            audience = _audience(args)
            visibility = args.get("visibility", "space")
            if visibility in ("public", "self"):
                scope = visibility
            else:
                scope = OWNER if audience.owner_present else space_scope(audience.space)
            m = core.add_memory(Memory(kind=args.get("kind", "fact"), text=args["text"], scope=scope,
                                       importance=float(args.get("importance", 0.5)), derived_by="mcp"))
            return {"id": m.id, "scope": m.scope}
        if name == "consolidate":
            r = core.consolidate()
            return {"episodes": r.episodes, "added": len(r.added), "updated": len(r.updated),
                    "rejected": len(r.rejected)}
        raise KeyError(name)  # pragma: no cover

    # ---------------------------------------------------------------- protocol

    def handle(self, msg: dict) -> dict | None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None:  # notification
            return None
        try:
            result = self._dispatch(method, params)
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                    "serverInfo": {"name": "memory-core", "version": __version__},
                    "instructions": "Long-term memory. Call `recall` before answering about past events or "
                                    "people; always pass the current `space`."}
        if method == "ping":
            return {}
        if method == "tools/list":
            tools = [t for t in TOOLS if not (t["write"] and self.read_only)]
            return {"tools": [{k: v for k, v in t.items() if k != "write"} for t in tools]}
        if method == "tools/call":
            name = params.get("name", "")
            try:
                value = self.call_tool(name, params.get("arguments") or {})
                return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}],
                        "isError": False}
            except Exception as exc:  # tool errors are reported to the model, not as protocol errors
                log.debug("tool %s failed: %s", name, traceback.format_exc())
                return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
        if method == "resources/list":
            return {"resources": [{"uri": f"identity://{n}", "name": f"{n}.md", "mimeType": "text/markdown"}
                                  for n in self.core.identity_names()]}
        if method == "resources/read":
            uri = params.get("uri", "")
            if not uri.startswith("identity://"):
                raise _RpcError(-32602, f"unknown resource {uri!r}")
            text = self.core.identity(uri.removeprefix("identity://"))
            if text is None:
                raise _RpcError(-32602, f"no such resource {uri!r}")
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": text}]}
        raise _RpcError(-32601, f"method not found: {method}")

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                response = self.handle(msg) if isinstance(msg, dict) else {
                    "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def run(path: str, *, read_only: bool = False) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)  # stdout is reserved for the protocol
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)
    stdout = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    with MemoryCore.open(path, create=False) as core:
        Server(core, read_only=read_only).serve(stdin, stdout)
    return 0
