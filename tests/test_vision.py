import io

import pytest

from memory_core.vision import UNSEEN, Seen, caption_block, clean, look, normalize, outline

PIL = pytest.importorskip("PIL.Image")


def _png(color=(255, 0, 0), size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    PIL.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class Viewer:
    def __init__(self, answer="一只橘猫趴在键盘上", fail=False):
        self.answer, self.fail, self.calls = answer, fail, 0

    def __call__(self, jpeg, prompt):
        self.calls += 1
        assert jpeg[:2] == b"\xff\xd8"  # always JPEG
        if self.fail:
            raise RuntimeError("down")
        return self.answer


def test_look_caches_by_content_and_falls_back(core):
    down, up = Viewer(fail=True), Viewer("<think>嗯</think> 一只橘猫\n趴在键盘上 ")
    errors = []
    seen = look(core, _png(), [("local", down), ("cloud", up)], errors=errors)
    assert seen.text == "一只橘猫 趴在键盘上" and seen.model == "cloud" and not seen.cached
    assert errors and errors[0].startswith("local")
    again = look(core, _png(), [("local", down), ("cloud", up)])
    assert again.cached and again.text == seen.text and up.calls == 1  # the same picture is looked at once
    other = look(core, _png((0, 0, 255)), [("cloud", up)])
    assert not other.cached and up.calls == 2


def test_nobody_can_see(core):
    assert look(core, _png(), [("a", Viewer(fail=True)), ("b", Viewer(""))]) is None
    assert look(core, _png(), []) is None


def test_keep_the_picture(core):
    seen = look(core, _png(), [("v", Viewer())], save=True)
    assert seen.saved.startswith("attachments/images/") and (core.root / seen.saved).exists()
    # seen before without saving: saving later keeps it without looking again
    v = Viewer()
    first = look(core, _png((1, 2, 3)), [("v", v)])
    assert not first.saved
    later = look(core, _png((1, 2, 3)), [("v", v)], save=True)
    assert later.saved and v.calls == 1


def test_normalize_animation_and_size():
    frames = [PIL.new("RGB", (3000, 1000), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])
    out = PIL.open(io.BytesIO(normalize(buf.getvalue(), 1280)))
    assert out.format == "JPEG" and max(out.size) == 1280
    assert normalize(b"not an image") == b"not an image"


def test_outline_and_caption():
    a, b = Seen("1", "橘猫", "v"), Seen("2", "截图", "v")
    assert outline("看 [图片] 和 [图片]", [a, b]) == "看 [图片：橘猫] 和 [图片：截图]"
    assert outline("[图片]", [None]) == UNSEEN
    assert outline("[图片] [图片]", [a]) == "[图片：橘猫] " + UNSEEN
    assert outline("你看", [a]) == "你看 [图片：橘猫]"
    block = caption_block([a, None])
    assert "图1：橘猫" in block and "别编" in block
    assert clean("“一张图”") == "一张图"
