import pytest

from memory_core import HashEmbedder, MemoryCore


@pytest.fixture
def core(tmp_path):
    c = MemoryCore.open(tmp_path / "soul", embedder=HashEmbedder(256), tokenizer="ngram")
    yield c
    c.close()


@pytest.fixture
def text_core(tmp_path):
    """No embedder: text and entity channels only."""
    c = MemoryCore.open(tmp_path / "soul-text", embedder=None, tokenizer="ngram")
    yield c
    c.close()
