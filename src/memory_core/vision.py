"""Seeing images: a vision model describes the picture once, the being remembers the description.

The being's own chat model may be text-only. Any vision model (local or remote, swappable, tried in order) tells
what is in the picture, plainly and without interpreting it; the being reacts in its own voice. Descriptions are
cached by the image's content, so the same sticker is looked at once. The original can be kept in `attachments/`.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

VISION_PROMPT_VERSION = "vision-v1"

VISION_PROMPT = """\
用中文把这张图讲给一个看不见它的人听：
- 先说它是什么：照片、截图、表情包、画、梗图……
- 画面里有什么、在发生什么；图里的文字原样写出来（字太多就挑要紧的）
- 只说看得见的，不猜发图的人想表达什么，不评价
三句话以内。"""

UNSEEN = "[图片，没看清]"
PLACEHOLDER = "[图片]"
META_PREFIX = "vision:"

# A viewer: (name, function that takes JPEG bytes and the prompt and returns the description).
Viewer = tuple[str, Callable[[bytes, str], str]]


@dataclass
class Seen:
    sha: str
    text: str
    model: str
    cached: bool = False
    saved: str = ""  # path relative to the being's root, when the picture itself was kept

    @property
    def note(self) -> str:
        return f"[图片：{self.text}]"


def image_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(data: bytes, max_side: int = 1280) -> bytes:
    """JPEG, first frame of an animation, longest side at most `max_side`. Unreadable data is returned as is."""
    try:
        from PIL import Image
    except ImportError:  # pillow is optional
        return data
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.seek(0)
            frame = img.convert("RGBA")
            canvas = Image.new("RGB", frame.size, (255, 255, 255))
            canvas.paste(frame, mask=frame.split()[-1])
            canvas.thumbnail((max_side, max_side))
            out = io.BytesIO()
            canvas.save(out, "JPEG", quality=85)
            return out.getvalue()
    except Exception:  # noqa: BLE001 - not an image Pillow understands
        return data


def clean(text: str, limit: int = 300) -> str:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    text = re.sub(r"\s+", " ", text).strip().strip("\"“”")
    return text[:limit].rstrip()


def cached(core, sha: str) -> Seen | None:
    raw = core.store.get_meta(META_PREFIX + sha)
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not d.get("text"):
        return None
    return Seen(sha=sha, text=d["text"], model=d.get("model", ""), cached=True, saved=d.get("saved", ""))


def _remember(core, seen: Seen) -> None:
    core.store.set_meta(META_PREFIX + seen.sha, json.dumps(
        {"text": seen.text, "model": seen.model, "saved": seen.saved, "prompt": VISION_PROMPT_VERSION,
         "at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False))


def keep(core, sha: str, jpeg: bytes, *, max_bytes: int = 2_000_000) -> str:
    """Keep the (normalized) picture in attachments/; returns its path relative to the root, or ''."""
    if len(jpeg) > max_bytes:
        return ""
    root = Path(core.root)
    rel = Path("attachments") / "images" / f"{sha[:2]}" / f"{sha}.jpg"
    path = root / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jpeg)
    return rel.as_posix()


def look(core, data: bytes, viewers: Sequence[Viewer], *, save: bool = False, prompt: str = VISION_PROMPT,
         max_side: int = 1280, errors: list[str] | None = None) -> Seen | None:
    """Describe one picture: the cache first, then each viewer in turn until one answers. None: nobody could."""
    sha = image_sha(data)
    seen = cached(core, sha)
    jpeg = b""
    if seen is None:
        jpeg = normalize(data, max_side)
        for name, see in viewers:
            try:
                text = clean(see(jpeg, prompt))
            except Exception as exc:  # noqa: BLE001 - try the next one
                if errors is not None:
                    errors.append(f"{name}: {exc}")
                continue
            if text:
                seen = Seen(sha=sha, text=text, model=name)
                break
        if seen is None:
            return None
    if save and not seen.saved:
        seen.saved = keep(core, sha, jpeg or normalize(data, max_side))
    if not seen.cached or seen.saved:
        _remember(core, seen)
    return seen


def outline(text: str, seen: Sequence[Seen | None]) -> str:
    """Put what was seen in place of each "[图片]" (in order); a picture nobody could see stays "[图片，没看清]"."""
    notes = [s.note if s is not None else UNSEEN for s in seen]
    parts = text.split(PLACEHOLDER)
    if len(parts) == 1:
        return " ".join([text, *notes]).strip() if notes else text
    out = [parts[0]]
    for i, rest in enumerate(parts[1:]):
        out.append(notes[i] if i < len(notes) else UNSEEN)
        out.append(rest)
    extra = notes[len(parts) - 1:]
    return ("".join(out) + (" " + " ".join(extra) if extra else "")).strip()


def caption_block(seen: Sequence[Seen | None]) -> str:
    """What the being sees in this message, for a text-only chat model."""
    lines = []
    for i, s in enumerate(seen, 1):
        lines.append(f"图{i}：{s.text}" if s is not None else f"图{i}：没看清（图片没能看成，别编内容）")
    return "<image_caption>\n" + "\n".join(lines) + "\n</image_caption>"
