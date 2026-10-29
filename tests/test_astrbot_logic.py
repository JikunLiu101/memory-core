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
