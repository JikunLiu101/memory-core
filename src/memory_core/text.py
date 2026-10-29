"""Tokenizers for the full-text index. The index is derived data: changing the tokenizer rebuilds it."""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

# CJK ideographs, kana, hangul.
_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_RUN = re.compile(f"[{_CJK}]+|[^\\W_{_CJK}]+", re.UNICODE)
_CJK_RUN = re.compile(f"^[{_CJK}]+$")


class Tokenizer(Protocol):
    id: str

    def tokenize(self, text: str) -> list[str]: ...


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


class NgramTokenizer:
    """Dependency-free: CJK runs become single characters + bigrams; other scripts become words."""

    id = "ngram-v1"

    def tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        for run in _RUN.findall(_normalize(text)):
            if _CJK_RUN.match(run):
                tokens.extend(run)
                tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
            else:
                tokens.append(run)
        return tokens


class JiebaTokenizer:
    """jieba search-mode segmentation (better Chinese words), plus single CJK characters."""

    id = "jieba-v1"

    def __init__(self) -> None:
        import warnings  # noqa: PLC0415

        with warnings.catch_warnings():  # jieba's own regexes trigger SyntaxWarning on Python 3.12+
            warnings.simplefilter("ignore", SyntaxWarning)
            import jieba  # noqa: PLC0415  (optional dependency)

        jieba.setLogLevel(60)
        self._jieba = jieba

    def tokenize(self, text: str) -> list[str]:
        normalized = _normalize(text)
        tokens: list[str] = []
        for word in self._jieba.cut_for_search(normalized):
            for run in _RUN.findall(word):
                tokens.append(run)
        for run in _RUN.findall(normalized):
            if _CJK_RUN.match(run):
                tokens.extend(run)
        return tokens


def get_tokenizer(name: str = "auto") -> Tokenizer:
    if name in ("auto", "jieba"):
        try:
            return JiebaTokenizer()
        except ImportError:
            if name == "jieba":
                raise
    if name in ("auto", "ngram"):
        return NgramTokenizer()
    raise ValueError(f"unknown tokenizer {name!r} (auto / jieba / ngram)")


def fts_query(tokens: list[str]) -> str | None:
    """Build an FTS5 OR-query from tokens; None if there is nothing to search for."""
    unique = list(dict.fromkeys(t for t in tokens if t))
    if not unique:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in unique)


def fts_document(tokens: list[str]) -> str:
    return " ".join(tokens)
