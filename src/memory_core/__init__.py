"""memory-core: a local-first, framework-agnostic long-term memory core for AI companions."""

__version__ = "0.6.0"

from .core import MemoryCore, RecallWeights  # noqa: E402
from .embed import Embedder, HashEmbedder, OpenAICompatibleEmbedder  # noqa: E402
from .models import (  # noqa: E402
    Entity,
    EntityKind,
    Episode,
    EpisodeKind,
    Memory,
    MemoryKind,
    MemoryStatus,
    RecallHit,
)
from .scope import OWNER, PUBLIC, SELF, Audience, space_scope  # noqa: E402

__all__ = [
    "OWNER",
    "PUBLIC",
    "SELF",
    "Audience",
    "Embedder",
    "Entity",
    "EntityKind",
    "Episode",
    "EpisodeKind",
    "HashEmbedder",
    "Memory",
    "MemoryCore",
    "MemoryKind",
    "MemoryStatus",
    "OpenAICompatibleEmbedder",
    "RecallHit",
    "RecallWeights",
    "space_scope",
    "__version__",
]
