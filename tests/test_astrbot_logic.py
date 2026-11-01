import importlib.util
import sys
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "adapters" / "astrbot" / "astrbot_plugin_memory_core" / "logic.py"
_spec = importlib.util.spec_from_file_location("astrbot_memory_logic", _path)
logic = importlib.util.module_from_spec(_spec)
sys.modules["astrbot_memory_logic"] = logic
_spec.loader.exec_module(logic)


def test_space_names():
    assert logic.space_of("aiocqhttp", True, "123", "9") == "aiocqhttp:group:123"
    assert logic.space_of("aiocqhttp", False, "", "9") == "aiocqhttp:private:9"
    assert logic.space_of("", False, None, "9") == "unknown:private:9"
    assert logic.actor_alias("webchat", "u1") == "webchat:u1"


def test_owner_detection():
    assert logic.is_owner_private(False, "42", ["42"])
    assert not logic.is_owner_private(True, "42", ["42"])
    assert not logic.is_owner_private(False, "7", ["42"])
    spaces = ["aiocqhttp:private:42", "aiocqhttp:group:42", "webchat:private:7", "webchat:private:42"]
    assert logic.owner_spaces(spaces, [42]) == ["aiocqhttp:private:42", "webchat:private:42"]


def test_is_command():
    assert logic.is_command("/mem_status", ["/"])
    assert not logic.is_command("我最喜欢明日方舟", ["/"])
    assert not logic.is_command("/x", [""])


def test_default_scope():
    assert logic.default_scope("aiocqhttp:group:1", False) == "space:aiocqhttp:group:1"
    assert logic.default_scope("aiocqhttp:private:42", True) == "owner"


def test_memory_block():
    assert logic.memory_block([]) == ""
    block = logic.memory_block(["阿明不吃香菜", "主人怕蛇"])
    assert block.startswith("<long_term_memory>") and "- 阿明不吃香菜" in block
    assert logic.memory_block(["x" * 50] * 10, max_chars=120).count("\n- ") <= 2


def test_identity_block_skips_empty_documents():
    docs = [("identity", "# who\nme\n"), ("self_image", None), ("voice_examples", "  ")]
    assert logic.identity_block(docs, guide="") == "<identity>\n# who\nme\n</identity>"
    assert logic.identity_block(docs).endswith("</how_your_memory_works>")
    assert logic.identity_block([]) == ""


def test_strip_framework_persona():
    system = "head\n# Persona Instructions\n\nYou are a helpful assistant.\n\nDEFAULT\ntail"
    out = logic.strip_framework_persona(system, "You are a helpful assistant.", ["DEFAULT"])
    assert "helpful" not in out and "DEFAULT" not in out
    assert out.startswith("head") and out.endswith("tail")
    assert logic.strip_framework_persona("unchanged", None) == "unchanged"


def test_speaker_note():
    assert "主人本人" in logic.speaker_note(True, "主人", "x", False)
    note = logic.speaker_note(False, "主人", "阿明", True)
    assert "阿明" in note and "不是主人" in note and "群聊" in note


def test_memory_guide_states_the_daily_schedule():
    guide = logic.memory_guide("老板", 5)
    assert "凌晨5点" in guide and "老板能看" in guide and "整理一次记忆" in guide
    assert "日记" not in logic.memory_guide(diary=False, tidy=False).split("memory_remember")[1]
    assert "每天凌晨4点左右还会整理" in logic.memory_guide(diary=False)


def test_owner_console_only_owner_private_on_listed_platform():
    owners, web = ["astrbot", "10001"], ["webchat"]
    assert logic.owner_console("webchat", False, "astrbot", owners, web)
    assert not logic.owner_console("webchat", True, "astrbot", owners, web)        # a group
    assert not logic.owner_console("webchat", False, "stranger", owners, web)      # someone else
    assert not logic.owner_console("aiocqhttp", False, "10001", owners, web)       # owner, unlisted platform
    assert logic.owner_console("aiocqhttp", False, "10001", owners, web + ["aiocqhttp"])
    assert not logic.owner_console(None, False, "astrbot", owners, [])


def test_strip_transient_keeps_history_stable():
    mem = logic.memory_block(["likes tea"])
    history = [
        {"role": "user", "content": [{"type": "text", "text": "hello"},
                                     {"type": "text", "text": "<system_reminder>Current datetime: x</system_reminder>"},
                                     {"type": "text", "text": mem},
                                     {"type": "text", "text": "<now>\n- tired\n</now>"}]},
        {"role": "assistant", "content": "hi <now>kept in replies</now>"},
        {"role": "user", "content": f"how are you\n\n{mem}"},
        {"role": "user", "content": [{"type": "text", "text": mem}]},
    ]
    assert logic.strip_transient(history) == 3
    assert [p["text"] for p in history[0]["content"]] == [
        "hello", "<system_reminder>Current datetime: x</system_reminder>"]
    assert history[1]["content"] == "hi <now>kept in replies</now>"
    assert history[2]["content"] == "how are you"
    assert len(history[3]["content"]) == 1  # never left empty
    first = [dict(m) if isinstance(m, dict) else m for m in history]
    assert logic.strip_transient(history) == 0 and history == first


def test_strip_transient_objects():
    class Part:
        def __init__(self, text):
            self.text = text

    class Msg:
        def __init__(self, role, content):
            self.role, self.content = role, content

    msg = Msg("user", [Part("hi"), Part("<long_term_memory>\nx\n</long_term_memory>")])
    assert logic.strip_transient([msg]) == 1 and [p.text for p in msg.content] == ["hi"]


def test_abilities_news_is_taken_out_of_the_history():
    history = [{"role": "user", "content": "在吗\n\n<abilities_news>\n- 刚有的本事：看图\n</abilities_news>"}]
    assert logic.strip_transient(history) == 1 and history[0]["content"] == "在吗"
