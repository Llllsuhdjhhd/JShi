from __future__ import annotations

import re


_ASCII_WORD = re.compile(r"[a-z0-9_]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def query_terms(text: str) -> frozenset[str]:
    """Return a small, deterministic term set for in-process recall ranking.

    The project currently avoids heavy embeddings for the minimal experiment.
    Chinese text is represented with both single characters and adjacent
    bigrams, while Latin text uses whole words. This is intentionally simple
    and replaceable; external memory engines may provide their own recall.
    """

    lowered = text.lower()
    terms: set[str] = set(_ASCII_WORD.findall(lowered))
    cjk = _CJK_CHAR.findall(lowered)
    terms.update(cjk)
    terms.update(
        "".join(cjk[index : index + 2])
        for index in range(len(cjk) - 1)
    )
    return frozenset(term for term in terms if term)
