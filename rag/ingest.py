"""
把爬取的原始游记 JSONL 处理后存入 Chroma 向量库。

Pipeline:
  raw JSONL → chunk → embed (bge-m3, 本地) → Chroma (本地持久化)

用法:
    pip install chromadb sentence-transformers
    python -m rag.ingest
"""

import hashlib
import json
import logging
from pathlib import Path

import chromadb
import numpy as np
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

RAW_DIR = Path(__file__).parent / "data" / "raw"
DB_DIR = Path(__file__).parent / "data" / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)

COLLECTION_NAME = "travel_notes"
EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
BATCH_SIZE = 64


def chunk_text(text: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + CHUNK_SIZE])
        start += CHUNK_SIZE - CHUNK_OVERLAP
    return chunks


def make_id(url: str, chunk_index: int) -> str:
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    return f"{url_hash}_{chunk_index}"


def load_raw_notes() -> list[dict]:
    notes = []
    for jsonl_file in RAW_DIR.glob("*.jsonl"):
        with open(jsonl_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    notes.append(json.loads(line))
    logger.info(f"Loaded {len(notes)} raw notes from {RAW_DIR}")
    return notes


def ingest():
    notes = load_raw_notes()
    if not notes:
        logger.error("No raw notes found. Run the crawler first.")
        return

    client = chromadb.PersistentClient(path=str(DB_DIR))
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    existing_ids = set(collection.get(include=[])["ids"])
    logger.info(f"Existing vectors in DB: {len(existing_ids)}")

    model = SentenceTransformer(EMBED_MODEL)

    # 准备所有新 chunks
    all_ids, all_texts, all_metas = [], [], []
    for note in notes:
        chunks = chunk_text(note["raw_text"])
        for i, chunk in enumerate(chunks):
            chunk_id = make_id(note["url"], i)
            if chunk_id in existing_ids:
                continue  # 断点续传，跳过已入库的
            all_ids.append(chunk_id)
            all_texts.append(chunk)
            all_metas.append({
                "url": note["url"],
                "title": note["title"],
                "region": note.get("region", ""),
                # Chroma metadata 不支持 list，tags 用逗号分隔存储
                "tags": ",".join(note.get("tags", [])),
                "budget_hint": note.get("budget_hint") or -1,
                "duration_days": note.get("duration_days") or -1,
                "chunk_index": i,
            })

    if not all_ids:
        logger.info("All notes already ingested. Nothing to do.")
        return

    logger.info(f"New chunks to embed: {len(all_ids)}")
    embeddings = model.encode(
        all_texts, batch_size=32, show_progress_bar=True, normalize_embeddings=True
    )

    # 分批写入 Chroma
    for i in range(0, len(all_ids), BATCH_SIZE):
        batch_ids = all_ids[i : i + BATCH_SIZE]
        batch_texts = all_texts[i : i + BATCH_SIZE]
        batch_metas = all_metas[i : i + BATCH_SIZE]
        batch_embs = embeddings[i : i + BATCH_SIZE].tolist()

        collection.add(
            ids=batch_ids,
            documents=batch_texts,
            embeddings=batch_embs,
            metadatas=batch_metas,
        )
        logger.info(f"Inserted batch {i // BATCH_SIZE + 1}, total so far: {min(i + BATCH_SIZE, len(all_ids))}")

    logger.info(f"Done. Total vectors in DB: {collection.count()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest()
