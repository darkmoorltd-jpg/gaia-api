import io
import os
import requests
from pypdf import PdfReader
from docx import Document as DocxDocument
from app.services.embeddings import embed_texts

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://pxvtvuwlpzwlkdoxjrep.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": "Bearer " + SUPABASE_SERVICE_KEY,
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

CHUNK_SIZE = 800       # characters
CHUNK_OVERLAP = 100
BATCH_SIZE = 16        # how many chunks to embed per HF call


def extract_text(file_bytes: bytes, filename: str) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        return "\n\n".join(pages)
    elif name.endswith(".docx"):
        doc = DocxDocument(io.BytesIO(file_bytes))
        return "\n\n".join(p.text for p in doc.paragraphs)
    elif name.endswith((".txt", ".md", ".csv", ".json")):
        return file_bytes.decode("utf-8", errors="ignore")
    else:
        raise ValueError("Unsupported file type: " + filename)


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP):
    """Split text into overlapping windows."""
    text = " ".join(text.split())  # collapse whitespace
    chunks = []
    i = 0
    while i < len(text):
        chunk = text[i:i + size]
        if len(chunk.strip()) > 20:
            chunks.append(chunk)
        i += size - overlap
    return chunks


def _post(path, payload):
    r = requests.post(SUPABASE_URL + path, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json() if r.content else None


def _get(path):
    r = requests.get(SUPABASE_URL + path, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def _delete(path):
    r = requests.delete(SUPABASE_URL + path, headers=HEADERS, timeout=30)
    r.raise_for_status()


def ingest_document(user_id: str, filename: str, file_bytes: bytes) -> dict:
    """Extract, chunk, embed, and store a document. Returns the created doc row."""
    # 1. Extract
    text = extract_text(file_bytes, filename)
    if not text.strip():
        raise ValueError("No readable text found in document")

    # 2. Chunk
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("Document too short to chunk")

    # 3. Create documents row
    doc_rows = _post("/rest/v1/documents", {
        "user_id": user_id,
        "filename": filename,
        "file_type": os.path.splitext(filename)[1].lstrip("."),
        "file_size": len(file_bytes),
        "page_count": len(chunks),
        "chunk_count": len(chunks),
    })
    doc_id = doc_rows[0]["id"]

    # 4. Embed in batches
    all_vectors = []
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i:i + BATCH_SIZE]
        vecs = embed_texts(batch)
        all_vectors.extend(vecs)

    # 5. Store chunks
    rows = []
    for i, (chunk, vec) in enumerate(zip(chunks, all_vectors)):
        rows.append({
            "document_id": doc_id,
            "user_id": user_id,
            "chunk_index": i,
            "content": chunk,
            "embedding": vec,
        })
    _post("/rest/v1/document_chunks", rows)

    return {"id": doc_id, "filename": filename, "chunk_count": len(chunks)}


def list_documents(user_id: str):
    return _get(
        "/rest/v1/documents?user_id=eq." + user_id
        + "&select=id,filename,file_type,file_size,page_count,chunk_count,created_at"
        + "&order=created_at.desc"
    )


def delete_document(user_id: str, doc_id: int):
    _delete("/rest/v1/documents?id=eq." + str(doc_id) + "&user_id=eq." + user_id)


def retrieve_context(user_id: str, question: str, document_ids=None, top_k: int = 5) -> str:
    """Embed the question, find top-k similar chunks, return joined text."""
    from app.services.embeddings import embed_one
    qvec = embed_one(question)
    if not qvec:
        return ""

    # If multiple documents are provided, run one search per document
    if document_ids:
        collected = []
        for did in document_ids:
            r = _post("/rest/v1/rpc/match_document_chunks", {
                "query_embedding": qvec,
                "match_user_id": user_id,
                "match_count": top_k,
                "filter_document_id": did,
            })
            collected.extend(r)
        chunks = collected
    else:
        chunks = _post("/rest/v1/rpc/match_document_chunks", {
            "query_embedding": qvec,
            "match_user_id": user_id,
            "match_count": top_k,
            "filter_document_id": None,
        })

    if not chunks:
        return ""

    chunks.sort(key=lambda c: -c.get("similarity", 0))
    chunks = chunks[:top_k * 2]

    parts = []
    for c in chunks:
        parts.append(c["content"])
    return "\n\n---\n\n".join(parts)[:12000]
