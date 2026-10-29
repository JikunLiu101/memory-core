import io
import json
import subprocess
import sys

from memory_core import Memory, MemoryCore
from memory_core.mcp_server import Server


def _rpc(server, method, params=None, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def _tool(server, name, **args):
    res = _rpc(server, "tools/call", {"name": name, "arguments": args})["result"]
    return res["isError"], (json.loads(res["content"][0]["text"]) if not res["isError"] else res["content"][0]["text"])


def test_initialize_and_list(core):
    s = Server(core)
    init = _rpc(s, "initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert init["protocolVersion"] == "2025-03-26"
    assert init["serverInfo"]["name"] == "memory-core"
    assert _rpc(s, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    names = {t["name"] for t in _rpc(s, "tools/list")["result"]["tools"]}
    assert {"recall", "record", "remember", "profile", "recent", "consolidate", "stats"} <= names
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc(s, "nope")["error"]["code"] == -32601


def test_tools_roundtrip_respects_scope(core):
    s = Server(core)
    err, rec = _tool(s, "record", content="主人说他怕蛇", space="qq:private:42", source="qq", kind="message_in",
                     actor="qq:42")
    assert not err and rec["id"]
    err, mem = _tool(s, "remember", text="主人怕蛇", space="qq:private:42", owner_present=True)
    assert mem["scope"] == "owner"
    _, hits = _tool(s, "recall", query="蛇", space="qq:group:1")
    assert hits == []
    _, hits = _tool(s, "recall", query="蛇", space="qq:private:42", owner_present=True)
    assert hits[0]["text"] == "主人怕蛇"
    _, recent = _tool(s, "recent", space="qq:private:42")
    assert recent[0]["content"] == "主人说他怕蛇"
    _, prof = _tool(s, "profile", entity="nobody", space="x")
    assert prof == {"found": False}


def test_profile(core):
    core.ensure_entity("qq:1001", name="阿明")
    core.add_memory(Memory(kind="fact", text="阿明玩原神", scope="space:g1", subjects=["qq:1001"]))
    s = Server(core)
    _, prof = _tool(s, "profile", entity="阿明", space="g1")
    assert prof["found"] and [m["text"] for m in prof["memories"]] == ["阿明玩原神"]
    _, prof = _tool(s, "profile", entity="阿明", space="g2")
    assert prof["memories"] == []


def test_read_only_hides_and_refuses_writes(core):
    s = Server(core, read_only=True)
    names = {t["name"] for t in _rpc(s, "tools/list")["result"]["tools"]}
    assert "record" not in names and "recall" in names
    err, text = _tool(s, "remember", text="x", space="g")
    assert err and "read-only" in text


def test_tool_errors_are_reported_not_raised(core):
    err, text = _tool(Server(core), "remember", text="x", space="g", kind="nonsense")
    assert err and "ValueError" in text


def test_resources(core):
    core.set_identity("self", "# 我\n")
    s = Server(core)
    assert _rpc(s, "resources/list")["result"]["resources"][0]["uri"] == "identity://self"
    assert _rpc(s, "resources/read", {"uri": "identity://self"})["result"]["contents"][0]["text"] == "# 我\n"
    assert "error" in _rpc(s, "resources/read", {"uri": "identity://nope"})


def test_serve_loop_handles_bad_lines(core):
    out = io.StringIO()
    Server(core).serve(io.StringIO('not json\n\n{"jsonrpc":"2.0","id":7,"method":"ping"}\n'), out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_stdio_subprocess(tmp_path):
    root = tmp_path / "soul"
    with MemoryCore.open(root, embedder=None) as c:
        c.add_memory(Memory(kind="fact", text="原神是游戏", scope="public"))
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "recall", "arguments": {"query": "原神", "space": "web"}}},
    ]
    proc = subprocess.run([sys.executable, "-m", "memory_core.cli", "mcp", str(root)],
                          input="\n".join(json.dumps(m, ensure_ascii=False) for m in msgs) + "\n",
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(x) for x in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2]
    assert "原神是游戏" in replies[1]["result"]["content"][0]["text"]
