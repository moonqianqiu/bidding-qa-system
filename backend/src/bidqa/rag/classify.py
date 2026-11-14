"""问题分类（docs/02 E1）：keyword / concept / mixed 三分类 → rrf_k（01 §4.1 检索参数表）。

规则：keyword 词表命中 ≥1 → keyword；concept 命中且 keyword==0 → concept；
两者都命中 → mixed；零命中 → keyword（默认低 rrf_k，信召回次序）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

_RRF_K = {"keyword": 30, "mixed": 60, "concept": 90}


@lru_cache(maxsize=1)
def _load_terms() -> tuple[tuple[str, ...], tuple[str, ...]]:
    path = Path(__file__).parent / "keywords.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(data["keyword_terms"]), tuple(data["concept_terms"])


def classify(question: str) -> tuple[str, int]:
    """返回 (类型, rrf_k)。"""
    kw_terms, cc_terms = _load_terms()
    q = question.lower()
    kw = sum(1 for t in kw_terms if t in q)
    cc = sum(1 for t in cc_terms if t in q)
    if kw > 0 and cc > 0:
        kind = "mixed"
    elif cc > 0:
        kind = "concept"
    else:
        kind = "keyword"
    return kind, _RRF_K[kind]
