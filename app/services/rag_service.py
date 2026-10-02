import os
import io
import re
import requests
from typing import List, Dict
import numpy as np

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

# ---- Embedding model (lazy-loaded) ----
_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _embedder


# ============================================
# TEXT EXTRACTION
# ============================================
def extract_text(contents: bytes, filename: str, mime: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "pdf" or "pdf" in mime:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(contents))
            return "\n\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            return f"[PDF extraction failed: {e}]"

    if ext in ("txt", "md") or "text" in mime:
        try:
            return contents.decode("utf-8", errors="ignore")
        except Exception:
            return ""

    if ext == "docx":
        try:
            import docx
            doc = docx.Document(io.BytesIO(contents))
            return "\n\n".join(p.text for p in doc.paragraphs)
        except Exception as e:
            return f"[DOCX extraction failed: {e}]"

    if ext in ("jpg", "jpeg", "png") or "image" in mime:
        # OCR not implemented server-side; return placeholder
        return "[Image uploaded — OCR not yet supported]"

    return contents.decode("utf-8", errors="ignore")


# ============================================
# CHUNKING (overlapping, sentence-aware)
# ============================================
def chunk_text(text: str, target_words: int = 220, overlap: int = 40) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    # Split into sentences (rough)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current: List[str] = []
    count = 0

    for s in sentences:
        w = len(s.split())
        if count + w > target_words and current:
            chunks.append(" ".join(current))
            # keep last "overlap" words for context
            tail = " ".join(current).split()[-overlap:]
            current = [" ".join(tail)]
            count = len(tail)
        current.append(s)
        count += w

    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if len(c.split()) >= 20]


# ============================================
# EMBEDDING + STORAGE
# ============================================
def embed(texts: List[str]) -> np.ndarray:
    model = get_embedder()
    return model.encode(texts, normalize_embeddings=True)


def insert_document(user_id: str, name: str, file_url: str, mime: str, size: int) -> str:
    r = requests.post(
        SUPABASE_URL + "/rest/v1/documents",
        headers=HEADERS,
        json={
            "user_id": user_id,
            "name": name,
            "file_url": file_url,
            "mime_type": mime,
            "size_bytes": size,
            "status": "processing",
        },
    )
    r.raise_for_status()
    return r.json()[0]["id"]


def insert_chunks(user_id: str, document_id: str, chunks: List[str]) -> int:
    if not chunks:
        return 0
    vecs = embed(chunks)
    rows = []
    for i, (chunk, vec) in enumerate(zip(chunks, vecs)):
        rows.append({
            "user_id": user_id,
            "document_id": document_id,
            "content": chunk,
            "chunk_index": i,
            "embedding": vec.tolist(),
        })

    # Bulk insert
    r = requests.post(
        SUPABASE_URL + "/rest/v1/document_chunks",
        headers=HEADERS,
        json=rows,
    )
    r.raise_for_status()
    return len(rows)


def finalize_document(doc_id: str, chunk_count: int):
    requests.patch(
        SUPABASE_URL + "/rest/v1/documents?id=eq." + doc_id,
        headers=HEADERS,
        json={"status": "ready", "chunk_count": chunk_count},
    )


# ============================================
# RETRIEVAL (vector similarity via RPC)
# ============================================
def retrieve(query: str, user_id: str, top_k: int = 5) -> List[Dict]:
    vec = embed([query])[0].tolist()
    r = requests.post(
        SUPABASE_URL + "/rest/v1/rpc/match_document_chunks",
        headers=HEADERS,
        json={
            "query_embedding": vec,
            "match_user_id": user_id,
            "match_count": top_k,
        },
    )
    if r.status_code != 200:
        return []
    return r.json() or []
