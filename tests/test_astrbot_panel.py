"""The AstrBot dashboard's Web API: argument checks, the routes it registers, and that it changes nothing."""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from memory_core import Memory

_path = Path(__file__).resolve().parents[1] / "adapters" / "astrbot" / "astrbot_plugin_memory_core" / "panel.py"
_spec = importlib.util.spec_from_file_location("astrbot_memory_panel", _path)
panel = importlib.util.module_from_spec(_spec)
sys.modules["astrbot_memory_panel"] = panel
_spec.loader.exec_module(panel)

OWNER_DM = "webchat:private:owner"


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, handler, methods))


class FakePlugin:
    def __init__(self, core):
        self.core = core
        self.cfg = {"inner_state": False, "self_name": "小星"}
        self.owner_title = "主人"
        self.context = FakeContext()

    def _abilities(self):
        return []

    def _owner_spaces(self):
        return [OWNER_DM]


class FakeRequest:
    def __init__(self, query=None, body=None):
        self.query = query or {}
        self._body = body

    async def json(self, default=None):
        return self._body if self._body is not None else default


def _call(p, monkeypatch, name, query=None, body=None):
    monkeypatch.setattr(panel.Panel, "_request", staticmethod(lambda: FakeRequest(query, body)))
    handler = dict((r.rsplit("/", 1)[1], h) for r, h, _ in p.plugin.context.routes)[name]
    return asyncio.run(handler())


def test_arguments_are_checked():
    a = panel.list_args({"statuses": "active,bogus", "limit": "9999", "offset": "-3", "kinds": ["fact", " "]})
    assert a["statuses"] == ["active"] and a["limit"] == 200 and a["offset"] == 0 and a["kinds"] == ["fact"]
    with pytest.raises(ValueError):
        panel.recall_args({"query": "  "})
    r = panel.recall_args({"query": "小星", "space": "qq:group:1", "owner_present": False, "k": "x"})
    assert r["audience"].space == "qq:group:1" and not r["audience"].owner_present and r["k"] == 8
    assert panel.graph_args({"statuses": "archived"})["statuses"] == ["archived"]
    assert panel.graph_args({})["statuses"] == ["active"]


def test_routes_and_answers(core, monkeypatch):
    core.ensure_entity("qq:1", name="小星")
    m = core.add_memory(Memory(kind="fact", text="小星喜欢猫", scope="owner", subjects=["qq:1"]))
    p = panel.Panel(FakePlugin(core))
    assert p.register()
    routes = {r: methods for r, _, methods in p.plugin.context.routes}
    assert routes[f"{panel.PREFIX}/memories"] == ["POST"] and routes[f"{panel.PREFIX}/recall"] == ["POST"]
    assert all(r.startswith("/astrbot_plugin_memory_core/page/") for r in routes)

    out = _call(p, monkeypatch, "overview")
    assert out["status"] == "ok" and out["data"]["owner_title"] == "主人"
    out = _call(p, monkeypatch, "memories", body={"text": "猫"})
    assert [i["id"] for i in out["data"]["items"]] == [m.id]
    out = _call(p, monkeypatch, "memory", query={"id": m.id})
    assert out["data"]["memory"]["text"] == "小星喜欢猫"
    assert _call(p, monkeypatch, "memory", query={"id": "nope"})["status"] == "error"
    assert _call(p, monkeypatch, "memory")["status"] == "error"
    out = _call(p, monkeypatch, "graph", query={"statuses": "active"})
    assert [e["name"] for e in out["data"]["entities"]] == ["小星"]
    entity = out["data"]["entities"][0]["id"]
    assert _call(p, monkeypatch, "entity", query={"id": entity})["data"]["memories"][0]["id"] == m.id
    assert _call(p, monkeypatch, "recall", body={"query": ""})["status"] == "error"

    before = core.memory(m.id)
    out = _call(p, monkeypatch, "recall", body={"query": "小星 猫", "space": OWNER_DM, "owner_present": True})
    assert [h["memory"]["id"] for h in out["data"]["hits"]] == [m.id]
    after = core.memory(m.id)
    assert (after.strength, after.last_recalled_at) == (before.strength, before.last_recalled_at)
    out = _call(p, monkeypatch, "recall", body={"query": "小星 猫", "space": "qq:group:9"})
    assert out["data"]["hits"] == []  # the owner's memory is not recalled in a group


def test_no_web_api_no_dashboard(core):
    plugin = FakePlugin(core)
    plugin.context = object()
    assert panel.Panel(plugin).register() is False
