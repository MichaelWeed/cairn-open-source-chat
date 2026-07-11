"""Embedded ChromaDB (PersistentClient, no server) — see MASTER_PLAN.md
task 2.1. Ingestion (chunking, content-hash reindex) lands in task 2.2;
this module only wires up the client, collection, and embedding function
selection.
"""

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection

from app.config import Settings
from app.embeddings import default_embedding_function

DOCUMENTS_COLLECTION_NAME = "documents"


def get_chroma_client(settings: Settings) -> ClientAPI:
    settings.chroma_path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(settings.chroma_path))


def get_document_collection(client: ClientAPI, settings: Settings) -> Collection:
    return client.get_or_create_collection(
        name=DOCUMENTS_COLLECTION_NAME,
        embedding_function=default_embedding_function(settings),
    )
