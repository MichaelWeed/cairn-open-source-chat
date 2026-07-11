from chromadb.api.types import Embeddings

from app.embeddings.fake import FakeEmbeddingFunction


def as_lists(embeddings: Embeddings) -> list[list[float]]:
    # The chromadb base class normalizes __call__'s return value into numpy
    # arrays; compare as plain lists instead of fighting array truthiness.
    return [list(v) for v in embeddings]


def test_deterministic() -> None:
    fn = FakeEmbeddingFunction()
    assert as_lists(fn(["hello"])) == as_lists(fn(["hello"]))


def test_different_inputs_yield_different_vectors() -> None:
    fn = FakeEmbeddingFunction()
    a, b = as_lists(fn(["hello", "goodbye"]))
    assert a != b


def test_dimension_is_consistent() -> None:
    fn = FakeEmbeddingFunction()
    vectors = as_lists(fn(["a", "bb", "ccc"]))
    dims = {len(v) for v in vectors}
    assert len(dims) == 1


def test_values_in_range() -> None:
    fn = FakeEmbeddingFunction()
    (vector,) = as_lists(fn(["some text"]))
    assert all(-1.0 <= v <= 1.0 for v in vector)


def test_name_and_config_round_trip() -> None:
    fn = FakeEmbeddingFunction()
    assert fn.name() == "fake"
    rebuilt = FakeEmbeddingFunction.build_from_config(fn.get_config())
    assert isinstance(rebuilt, FakeEmbeddingFunction)
