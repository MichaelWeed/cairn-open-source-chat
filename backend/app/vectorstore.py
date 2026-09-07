"""Embedded SQLite flat-vector index for the document collection."""

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Any, TypedDict

from app.config import Settings
from app.embedding_types import EmbeddingFunction, EmbeddingVector, Metadata
from app.embeddings import default_embedding_function

DOCUMENTS_COLLECTION_NAME = "documents"
DATABASE_FILENAME = "cairn-vectors-v1.sqlite3"
SCHEMA_VERSION = 1


class GetResult(TypedDict):
    ids: list[str]
    documents: list[str]
    metadatas: list[Metadata]


class QueryResult(TypedDict):
    ids: list[list[str]]
    documents: list[list[str]]
    metadatas: list[list[Metadata]]
    distances: list[list[float]]


class VectorStoreClient:
    """Owns a local SQLite connection and creates collection-scoped handles."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._lock = RLock()
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError(f"unsupported vector-store schema version: {version}")
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    collection_name TEXT NOT NULL,
                    id TEXT NOT NULL,
                    document TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    PRIMARY KEY (collection_name, id)
                )
                """
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def heartbeat(self) -> None:
        with self._lock:
            self._connection.execute("SELECT 1").fetchone()

    def get_or_create_collection(
        self, *, name: str, embedding_function: EmbeddingFunction
    ) -> "DocumentCollection":
        return DocumentCollection(
            connection=self._connection,
            lock=self._lock,
            name=name,
            embedding_function=embedding_function,
        )


class DocumentCollection:
    """Synchronous, collection-scoped vector operations used by Cairn."""

    def __init__(
        self,
        *,
        connection: sqlite3.Connection,
        lock: RLock,
        name: str,
        embedding_function: EmbeddingFunction,
    ) -> None:
        self._connection = connection
        self._lock = lock
        self._name = name
        self._embedding_function = embedding_function

    def add(
        self,
        *,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[Metadata] | None = None,
    ) -> None:
        rows = self._prepared_rows(ids=ids, documents=documents, metadatas=metadatas)
        if not rows:
            return
        with self._lock:
            placeholders = ",".join("?" * len(rows))
            existing = self._connection.execute(
                "SELECT id FROM vectors WHERE collection_name = ? " f"AND id IN ({placeholders})",
                (self._name, *(row[1] for row in rows)),
            ).fetchone()
            if existing is not None:
                raise ValueError(f"ID already exists: {existing[0]}")
            with self._connection:
                self._insert_rows(rows)

    def replace(
        self,
        *,
        ids_to_delete: Sequence[str],
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[Metadata] | None = None,
    ) -> None:
        """Atomically replace a document's chunk rows after embeddings are ready."""
        rows = self._prepared_rows(ids=ids, documents=documents, metadatas=metadatas)
        deleted_ids = list(ids_to_delete)
        placeholders = ",".join("?" * len(deleted_ids))
        with self._lock:
            with self._connection:
                if deleted_ids:
                    self._connection.execute(
                        "DELETE FROM vectors WHERE collection_name = ? "
                        f"AND id IN ({placeholders})",
                        (self._name, *deleted_ids),
                    )
                if rows:
                    self._insert_rows(rows)

    def delete(self, *, ids: Sequence[str]) -> None:
        id_values = list(ids)
        if not id_values:
            return
        placeholders = ",".join("?" * len(id_values))
        with self._lock:
            with self._connection:
                self._connection.execute(
                    "DELETE FROM vectors WHERE collection_name = ? " f"AND id IN ({placeholders})",
                    (self._name, *id_values),
                )

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM vectors WHERE collection_name = ?", (self._name,)
            ).fetchone()
        return int(row[0])

    def get(self, *, ids: Sequence[str]) -> GetResult:
        id_values = list(ids)
        with self._lock:
            rows = {
                row[0]: row[1:]
                for identifier in id_values
                if (
                    row := self._connection.execute(
                        "SELECT id, document, metadata FROM vectors "
                        "WHERE collection_name = ? AND id = ?",
                        (self._name, identifier),
                    ).fetchone()
                )
                is not None
            }
        return {
            "ids": [identifier for identifier in id_values if identifier in rows],
            "documents": [
                str(rows[identifier][0]) for identifier in id_values if identifier in rows
            ],
            "metadatas": [
                self._decode_metadata(str(rows[identifier][1]))
                for identifier in id_values
                if identifier in rows
            ],
        }

    def query(self, *, query_texts: Sequence[str], n_results: int) -> QueryResult:
        query_values = list(query_texts)
        query_embeddings = self._embedding_function(query_values)
        if len(query_embeddings) != len(query_values):
            raise ValueError("embedding function returned an unexpected vector count")
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, document, metadata, embedding FROM vectors WHERE collection_name = ?",
                (self._name,),
            ).fetchall()
        result_count = min(max(n_results, 0), len(rows))
        result: QueryResult = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        for query_embedding in query_embeddings:
            scored = sorted(
                (
                    (
                        self._squared_l2(query_embedding, self._decode_embedding(str(row[3]))),
                        str(row[0]),
                        str(row[1]),
                        self._decode_metadata(str(row[2])),
                    )
                    for row in rows
                ),
                key=lambda item: (item[0], item[1]),
            )[:result_count]
            result["ids"].append([item[1] for item in scored])
            result["documents"].append([item[2] for item in scored])
            result["metadatas"].append([item[3] for item in scored])
            result["distances"].append([item[0] for item in scored])
        return result

    @staticmethod
    def _validated_embedding(embedding: Sequence[float]) -> EmbeddingVector:
        values = list(embedding)
        if not values or not all(isinstance(value, int | float) for value in values):
            raise ValueError("embedding must be a non-empty numeric vector")
        return [float(value) for value in values]

    def _prepared_rows(
        self,
        *,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[Metadata] | None,
    ) -> list[tuple[str, str, str, str, str]]:
        if len(ids) != len(documents):
            raise ValueError("ids and documents must have equal lengths")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate IDs are not allowed")
        id_values = list(ids)
        document_values = list(documents)
        metadata_values = list(metadatas) if metadatas is not None else [{} for _ in ids]
        if len(metadata_values) != len(id_values):
            raise ValueError("ids and metadatas must have equal lengths")
        if not id_values:
            return []
        embeddings = self._embedding_function(document_values)
        if len(embeddings) != len(id_values):
            raise ValueError("embedding function returned an unexpected vector count")
        return [
            (
                self._name,
                identifier,
                document,
                json.dumps(metadata),
                json.dumps(self._validated_embedding(embedding)),
            )
            for identifier, document, metadata, embedding in zip(
                id_values, document_values, metadata_values, embeddings, strict=True
            )
        ]

    def _insert_rows(self, rows: Sequence[tuple[str, str, str, str, str]]) -> None:
        self._connection.executemany(
            "INSERT INTO vectors (collection_name, id, document, metadata, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )

    @classmethod
    def _decode_embedding(cls, encoded: str) -> EmbeddingVector:
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError("stored embedding is malformed") from error
        if not isinstance(value, list):
            raise ValueError("stored embedding is malformed")
        return cls._validated_embedding(value)

    @staticmethod
    def _decode_metadata(encoded: str) -> Metadata:
        try:
            value: Any = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise ValueError("stored metadata is malformed") from error
        if not isinstance(value, dict):
            raise ValueError("stored metadata is malformed")
        return value

    @staticmethod
    def _squared_l2(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right):
            raise ValueError("vector dimensions must match")
        return sum((float(a) - float(b)) ** 2 for a, b in zip(left, right, strict=True))


def get_vector_client(settings: Settings) -> VectorStoreClient:
    return VectorStoreClient(settings.chroma_path / DATABASE_FILENAME)


def get_document_collection(client: VectorStoreClient, settings: Settings) -> DocumentCollection:
    return client.get_or_create_collection(
        name=DOCUMENTS_COLLECTION_NAME,
        embedding_function=default_embedding_function(settings),
    )
