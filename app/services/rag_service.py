import os
import io
import re
from typing import List, Dict
import numpy as np


def _supabase_url() -> str:
    return os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")


def _supabase_key() -> str:
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_KEY not set")
    return key


def _headers() -> dict:
    key = _supabase_key()
    return {
        "apikey": key,
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return _embedder


def extract_text(contents: bytes, filename: str, mime: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "pdf" or "pdf" in mime:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(contents))
            return "\n\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            return "[PDF extraction failed: " + str(e) + "]"

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
            return "[DOCX extraction failed: " + str(e) + "]"

    if ext in ("jpg", "jpeg", "png") or "image" in mime:
        return "[Image uploaded - OCR not yet supported]"

    return contents.decode("utf-8", errors="ignore")


def chunk_text(text: str, target_words: int = 220, overlap: int = 40) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip(
()
    if not text:
        return []

    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks = []
    current = []
    count = 0

    for s in sentences:
        w = len(s.split())
        if count + w > target_words and current:
            chunks.append(" ".join(current))
            tail = " ".join(current).split()[-overlap:]
            current = [" ".join(tail)]
            count = len(tail)
        current.append(s)
        count += w

    if current:
        chunks.append(" ".join(current))

    return [c for c in chunks if len(c.split()) >= 20]


def embed(texts: List[str]) -> np.ndarray:
    model = get_embedder()
    return model.encode(texts, normalize_embeddings=True)


def insert_document(user_id: str, name: str, file_url: str, mime: str, size: int) -> str:
    import requests
    r = requests.post(
        _supabase_url() + "/rest/v1/documents",
        headers=_headers(),
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
    import requests
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

    r = requests.post(
        _supabase_url() + "/rest/v1/document_chunks",
        headers=_headers(),
        json=rows,
    )
    if r.status_code not in (200, 201):
        print("CHUNK INSERT FAILED:", r.status_code, r.text[:300], flush=True)
    r.raise_for_status()
    return len(rows)


def finalize_document(doc_id: str, chunk_count: int):
    import requests
    requests.patch        _supabase_url() + "/rest/v1/documents?id=eq." + doc_id,
        headers=_headers(),
        json={"status": "ready", "chunk_count": chunk_count},
    )


def retrieve(query: str, user_id: str, top_k: int = 5) -> List[Dict]:
    """Vector similarity search with verbose logging."""
    import requests

    vec = embed([query])[0].tolist()
    print("RETRIEVE query=" + query[:50] + " user_id=" + user_id + " vec_dim=" + str(len(vec)), flush=True)

    # ---- Try RPC ----
    r = requests.post(
        _supabase_url() + "/rest/v1/rpc/match_document_chunks",
        headers=_headers(),
        json={
            "query_embedding": vec,
            "match_user_id": user_id,
            "match_count": top_k,
        },
    )
    print("RPC STATUS:", r.status_code, flush=True)

    if r.status_code == 200:
        rows = r.json() or []
        print("RPC RETURNED:", len(rows), "rows", flush=True)
        if rows:
            return rows

    # ---- RPC failed or empty: fall back to direct table scan ----
    print("RPC empty/failed. Falling back to direct chunk scan.", flush=True)
    fallback = requests.get(
        _supabase_url() + "/rest/v1/document_chunks?user_id=eq." + user_id + "&select=id,document_id,content&limit=5",
        headers=_headers(),
    )
    if fallback.status_code == 200:
        rows = fallback.json() or []
        print("FALLBACK RETURNED:", len(rows), "rows", flush=True)
        return rows
    else:
        print("FALLBACK FAILED:", fallback.status_code, fallback.text[:200], flush=True)
        return []
