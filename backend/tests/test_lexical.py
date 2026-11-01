"""S3: lexical (sparse) vectorization — CJK bigram + Latin tokens, tf·idf weights."""

from harbor.features.catalog.search.lexical import LexicalVectorizer, sparse_dot, terms


def test_terms_bigrams_cjk_and_keeps_latin() -> None:
    assert terms("轻便吸尘器") == ["轻便", "便吸", "吸尘", "尘器"]
    assert terms("养猫") == ["养猫"]
    assert terms("吸") == ["吸"]  # a single CJK char stays a unigram
    assert terms("Nomadica 轻便") == ["nomadica", "轻便"]


def test_vectorizer_encodes_character_overlap() -> None:
    vec = LexicalVectorizer().fit(["轻便吸尘器", "养猫用品"])
    # "轻便" shares the 轻便 bigram with "轻便吸尘器"
    assert sparse_dot(vec.encode("轻便"), vec.encode("轻便吸尘器")) > 0
    # "养猫" shares no bigram with "轻便吸尘器"
    assert sparse_dot(vec.encode("养猫"), vec.encode("轻便吸尘器")) == 0


def test_encode_returns_sorted_indices() -> None:
    vec = LexicalVectorizer().fit(["吸尘器 吸尘器", "台灯"])
    indices, values = vec.encode("吸尘器")
    assert indices == sorted(indices)
    assert all(value > 0 for value in values)


def test_unknown_terms_are_ignored() -> None:
    vec = LexicalVectorizer().fit(["台灯"])
    assert vec.encode("不存在的词") == ([], [])


def test_sparse_dot_counts_only_overlapping_indices() -> None:
    a = ([0, 2], [1.0, 3.0])
    b = ([1, 2], [2.0, 4.0])
    assert sparse_dot(a, b) == 12.0  # only index 2 overlaps: 3.0 * 4.0
