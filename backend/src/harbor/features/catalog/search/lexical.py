"""Chinese-aware lexical (sparse) vectorization for hybrid recall (S3).

Qdrant's built-in BM25 tokenizer splits on whitespace, which leaves CJK text as
one opaque token — "轻便吸尘器" would never match a query for "吸尘器". This
module instead encodes text as CJK character bigrams plus lowercase Latin/digit
tokens, so a query and a title that overlap only in characters still match
lexically. Zero dependencies, deterministic, and shared by the Qdrant indexer and
the reference pipeline so both produce identical sparse vectors.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence

# Latin/digit runs stay whole tokens; a maximal run of non-ASCII, non-space chars
# (CJK) is broken into overlapping bigrams.
_LATIN_RUN = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[^\x00-\x7f\s]+")

# ``(indices, values)`` of a sparse vector, indices ascending, values tf·idf.
SparseWeights = tuple[list[int], list[float]]


def terms(text: str) -> list[str]:
    """Tokenize into CJK bigrams + lowercase Latin/digit tokens."""
    lowered = text.lower()
    result = list(_LATIN_RUN.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            result.append(run)
        else:
            result.extend(run[i : i + 2] for i in range(len(run) - 1))
    return result


class LexicalVectorizer:
    """Fits a term → index/idf map over a corpus and encodes texts as sparse weights.

    Weights are tf·idf (BM25-style idf) computed client-side, so Qdrant stores the
    vector with no server-side IDF modifier and its dot product equals exactly the
    sum the reference pipeline computes in pure Python.
    """

    def __init__(self) -> None:
        self._term_id: dict[str, int] = {}
        self._idf: dict[int, float] = {}

    def fit(self, texts: Sequence[str]) -> "LexicalVectorizer":
        document_frequency: Counter[int] = Counter()
        term_id: dict[str, int] = {}
        for text in texts:
            seen: set[int] = set()
            for term in terms(text):
                idx = term_id.setdefault(term, len(term_id))
                if idx not in seen:
                    document_frequency[idx] += 1
                    seen.add(idx)
        count = len(texts)
        self._term_id = term_id
        self._idf = {
            idx: math.log((count + 1) / (frequency + 1)) + 1
            for idx, frequency in document_frequency.items()
        }
        return self

    def encode(self, text: str) -> SparseWeights:
        """Encode ``text`` as ``(indices, values)``, indices ascending, values tf·idf."""
        counts: Counter[int] = Counter()
        for term in terms(text):
            idx = self._term_id.get(term)
            if idx is not None:
                counts[idx] += 1
        indices = sorted(counts)
        values = [counts[i] * self._idf[i] for i in indices]
        return indices, values

    def __len__(self) -> int:
        return len(self._term_id)


def sparse_dot(a: SparseWeights, b: SparseWeights) -> float:
    """Dot product of two sparse weight vectors — Qdrant's sparse-search score."""
    a_indices, a_values = a
    b_lookup = dict(zip(b[0], b[1], strict=True))
    return sum(
        value * b_lookup[i] for i, value in zip(a_indices, a_values, strict=True) if i in b_lookup
    )
