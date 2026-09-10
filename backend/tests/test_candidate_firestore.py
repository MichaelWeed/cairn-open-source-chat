import importlib
import socket
from collections.abc import AsyncGenerator, Mapping
from typing import Any

import pytest

from app.ingest.candidate_firestore import (
    FirestoreSdkCandidateStore,
    create_candidate_store,
)
from app.ingest.candidate_persistence import (
    CANDIDATE_COLLECTION,
    CANDIDATE_READ_PAGE_SIZE,
    CandidateStoreFailure,
    CandidateStoreRecord,
)
from app.retrieval_contracts import ExactCorpusReference
from app.retrieval_firestore import FIRESTORE_CHUNKS_COLLECTION


class _FakeProto:
    def __init__(self, material: bytes) -> None:
        self._material = material

    def ByteSize(self) -> int:
        return len(self._material)

    def SerializeToString(self) -> bytes:
        return self._material


class _FakeDocument:
    def __init__(self, material: bytes) -> None:
        self._pb = _FakeProto(material)


class _FakeWrite:
    def __init__(self, path: str, value: Mapping[str, object]) -> None:
        material = (path + repr(sorted(value.items()))).encode()
        self.update = _FakeDocument(material)
        self._pb = _FakeProto(material)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _FakeWrite)
            and self._pb.SerializeToString() == other._pb.SerializeToString()
        )


class _FakeHelpers:
    @staticmethod
    def pbs_for_create(path: str, value: Mapping[str, object]) -> list[_FakeWrite]:
        return [_FakeWrite(path, value)]


class _FakeCommitRequest:
    def __init__(self, *, database: str, writes: tuple[_FakeWrite, ...]) -> None:
        material = database.encode() + b"".join(
            write._pb.SerializeToString() for write in writes
        )
        self._pb = _FakeProto(material)


class _FakeVector:
    def __init__(self, value: list[float]) -> None:
        self._value = tuple(value)

    def __repr__(self) -> str:
        return f"FakeVector({self._value!r})"


class _FakeBaseQuery:
    @staticmethod
    def FieldFilter(field: str, operator: str, value: str) -> tuple[str, str, str]:
        return field, operator, value

    @staticmethod
    def And(filters: list[object]) -> tuple[object, ...]:
        return tuple(filters)


class _FakeAlreadyExists(Exception):
    pass


class _FakeSdk:
    class vector:
        Vector = _FakeVector

    class types:
        CommitRequest = _FakeCommitRequest

    base_query = _FakeBaseQuery


class _FakeExceptions:
    AlreadyExists = _FakeAlreadyExists


try:
    firestore_v1: Any = importlib.import_module("google.cloud.firestore_v1")
    _helpers: Any = importlib.import_module("google.cloud.firestore_v1._helpers")
    exceptions: Any = importlib.import_module("google.api_core.exceptions")
    _HAS_REAL_SDK = True
except ModuleNotFoundError:
    firestore_v1 = _FakeSdk()
    _helpers = _FakeHelpers()
    exceptions = _FakeExceptions()
    _HAS_REAL_SDK = False


class Snapshot:
    def __init__(self, key: str, value: Mapping[str, object] | None) -> None:
        self.id = key
        self.exists = value is not None
        self._value = value

    def to_dict(self) -> Mapping[str, object] | None:
        return self._value


class Reference:
    def __init__(self, client: "Client", collection: str, key: str) -> None:
        self._client = client
        self.collection = collection
        self.id = key
        self._document_path = (
            f"projects/test-project/databases/(default)/documents/{collection}/{key}"
        )

    async def get(self, *, retry: None, timeout: int) -> Snapshot:
        self._client.calls.append(("get", self.collection, self.id, retry, timeout))
        return Snapshot(self.id, self._client.records.get((self.collection, self.id)))


class Query:
    def __init__(self, client: "Client", collection: str) -> None:
        self.client = client
        self.collection = collection
        self.after: str | None = None
        self.limit_count = 0

    def where(self, *, filter: object) -> "Query":
        self.client.calls.append(("where", self.collection, filter))
        return self

    def order_by(self, field: str) -> "Query":
        self.client.calls.append(("order_by", field))
        return self

    def start_after(self, value: Mapping[str, Reference]) -> "Query":
        self.after = value["__name__"].id
        self.client.calls.append(("start_after", self.after))
        return self

    def limit(self, value: int) -> "Query":
        self.limit_count = value
        self.client.calls.append(("limit", value))
        return self

    async def get(self, *, retry: None, timeout: int) -> list[Snapshot]:
        self.client.calls.append(("query_get", retry, timeout))
        items = sorted(
            (
                (key, value)
                for (collection, key), value in self.client.records.items()
                if collection == self.collection
                and (self.after is None or key.encode() > self.after.encode())
            ),
            key=lambda item: item[0].encode(),
        )
        return [Snapshot(key, value) for key, value in items[: self.limit_count]]


class Collection:
    def __init__(self, client: "Client", name: str) -> None:
        self.client = client
        self.name = name

    def document(self, key: str) -> Reference:
        return Reference(self.client, self.name, key)

    def where(self, *, filter: object) -> Query:
        return Query(self.client, self.name).where(filter=filter)


class Batch:
    def __init__(self, client: "Client") -> None:
        self.client = client
        self._write_pbs: list[object] = []
        self.pending: list[tuple[Reference, Mapping[str, object]]] = []

    def create(self, reference: Reference, value: Mapping[str, object]) -> None:
        self.pending.append((reference, value))
        self._write_pbs.extend(_helpers.pbs_for_create(reference._document_path, dict(value)))

    async def commit(self, *, retry: None, timeout: int) -> None:
        self.client.calls.append(("commit", retry, timeout, len(self.pending)))
        if self.client.failure is not None:
            raise self.client.failure
        for reference, value in self.pending:
            identity = (reference.collection, reference.id)
            if identity in self.client.records:
                raise exceptions.AlreadyExists("fixture")
            self.client.records[identity] = value


class Client:
    _database_string = "projects/test-project/databases/(default)"

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], Mapping[str, object]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.failure: Exception | None = None
        self.closed = 0

    def collection(self, name: str) -> Collection:
        return Collection(self, name)

    def batch(self) -> Batch:
        return Batch(self)

    async def get_all(
        self, references: list[Reference], *, retry: None, timeout: int
    ) -> AsyncGenerator[Snapshot, None]:
        self.calls.append(("get_all", retry, timeout, tuple(ref.id for ref in references)))
        for reference in reversed(references):
            yield Snapshot(
                reference.id,
                self.records.get((reference.collection, reference.id)),
            )

    def close(self) -> None:
        self.closed += 1


def _store(client: Client) -> FirestoreSdkCandidateStore:
    return FirestoreSdkCandidateStore(client, sdk=firestore_v1, helpers=_helpers)


def _header() -> CandidateStoreRecord:
    return CandidateStoreRecord(
        kind="candidate",
        key="cand1-" + "a" * 64,
        value={
            "schema_version": "1.0",
            "corpus_id": "public-docs",
            "corpus_version": "v1",
        },
    )


def _chunk(key: str = "c1-" + "b" * 64) -> CandidateStoreRecord:
    return CandidateStoreRecord(
        kind="chunk",
        key=key,
        value={
            "schema_version": "1.0",
            "corpus_id": "public-docs",
            "corpus_version": "v1",
            "embedding_identity": "fixture",
            "chunk_id": "chk_" + "c" * 64,
            "document_id": "doc_" + "d" * 64,
            "source": "guide.md",
            "chunk_index": 0,
            "text": "content",
            "citation_title": "Guide",
            "citation_url": "https://example.test/guide",
            "embedding": (1.0, 0.0),
        },
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: object, **__: object) -> Any:
        raise AssertionError("network forbidden in candidate Firestore tests")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.mark.asyncio
async def test_exact_size_create_read_and_vector_round_trip() -> None:
    client = Client()
    store = _store(client)
    header = _header()
    chunk = _chunk()
    assert 0 < store.encoded_document_size(header) < 1_048_576
    assert 0 < store.encoded_create_size("candidate", (header,)) < 8_388_608
    await store.create_many("candidate", (header,), timeout_seconds=7)
    assert await store.get("candidate", header.key, timeout_seconds=7) == header

    store.encoded_document_size(chunk)
    store.encoded_create_size("chunk", (chunk,))
    await store.create_many("chunk", (chunk,), timeout_seconds=7)
    assert await store.get("chunk", chunk.key, timeout_seconds=7) == chunk
    assert client.calls[-1] == (
        "get",
        FIRESTORE_CHUNKS_COLLECTION,
        chunk.key,
        None,
        7,
    )


@pytest.mark.asyncio
async def test_create_requires_prevalidated_exact_bytes_and_normalizes_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client()
    store = _store(client)
    header = _header()
    with pytest.raises(CandidateStoreFailure) as uncached:
        await store.create_many("candidate", (header,), timeout_seconds=3)
    assert uncached.value.code == "permanent"

    store.encoded_create_size("candidate", (header,))
    monkeypatch.setattr(
        "app.ingest.candidate_firestore.importlib.import_module",
        lambda _: exceptions,
    )
    client.failure = exceptions.AlreadyExists("fixture")
    with pytest.raises(CandidateStoreFailure) as conflict:
        await store.create_many("candidate", (header,), timeout_seconds=3)
    assert conflict.value.code == "conflict"


@pytest.mark.asyncio
async def test_get_many_restores_requested_order_and_close_is_idempotent() -> None:
    client = Client()
    store = _store(client)
    first = _header()
    second = CandidateStoreRecord(
        kind="candidate", key="cand1-" + "b" * 64, value=dict(first.value)
    )
    for record in (first, second):
        client.records[(CANDIDATE_COLLECTION, record.key)] = dict(record.value)
    assert await store.get_many(
        "candidate", (first.key, second.key), timeout_seconds=5
    ) == (first, second)
    await store.aclose()
    await store.aclose()
    assert client.closed == 1


@pytest.mark.asyncio
async def test_page_uses_exact_filters_name_order_and_201_lookahead() -> None:
    client = Client()
    store = _store(client)
    corpus = ExactCorpusReference(corpus_id="public-docs", corpus_version="v1")
    for index in range(201):
        record = _chunk(f"c1-{index:064x}")
        client.records[(FIRESTORE_CHUNKS_COLLECTION, record.key)] = {
            **dict(record.value),
            "embedding": firestore_v1.vector.Vector([1.0, 0.0]),
        }
    page = await store.list_page(
        "chunk", corpus, None, CANDIDATE_READ_PAGE_SIZE, timeout_seconds=9
    )
    assert len(page.records) == 200
    assert page.next_after_key == page.records[-1].key
    assert ("order_by", "__name__") in client.calls
    assert ("limit", 201) in client.calls
    assert ("query_get", None, 9) in client.calls


def test_factory_is_construct_only_and_missing_profile_is_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _HAS_REAL_SDK:
        with pytest.raises(RuntimeError, match="optional 'firestore' dependency"):
            create_candidate_store("test-project")
        return
    calls: list[tuple[object, ...]] = []

    class ConstructedClient(Client):
        def __init__(self, *, project: str, database: str) -> None:
            super().__init__()
            calls.append((project, database))

    monkeypatch.setattr(firestore_v1, "AsyncClient", ConstructedClient)
    created = create_candidate_store("test-project")
    assert isinstance(created, FirestoreSdkCandidateStore)
    assert calls == [("test-project", "(default)")]
