import chromadb
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
from chromadb.config import Settings

# vectorstore/chroma_store.py
import chromadb
from chromadb.config import Settings
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
import os

BASE_DIR = "/Users/thanhhai/itec"
CHROMA_PATH = os.path.join(BASE_DIR, "chroma_db")

client = chromadb.PersistentClient(
    path=CHROMA_PATH
)

embedding_function = ONNXMiniLM_L6_V2(
    preferred_providers=["CPUExecutionProvider"]
)

def create_collection(name: str):
    return client.get_or_create_collection(
        name=name,
        embedding_function=embedding_function
    )

def delete_collection(name: str):
    client.delete_collection(name=name)


def add_in_batches(collection, documents, metadatas, ids, batch_size=500):
    for i in range(0, len(documents), batch_size):
        collection.add(
            documents=documents[i:i+batch_size],
            metadatas=metadatas[i:i+batch_size],
            ids=ids[i:i+batch_size]
        )