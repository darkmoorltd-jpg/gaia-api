"""Embeddings for GAIA RAG.

Priority:
1. sentence-transformers (local, no API key needed) — recommended
2. HuggingFace Inference API (if HF_TOKEN set)
3. Hash fallback (deterministic, keyword-quality only)
"""
import os
import hashlib

_MODEL = None
_EMBED_DIM = 384
_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer
        print("[embeddings] Loading sentence-transformers model…")
        _MODEL = SentenceTransformer(_MODEL_NAME)
        print("[embeddings] Model loaded.")
        return _MODEL
    except Exception as e:
        print("[embeddings] sentence-transformers unavailable:", e)
        return None


def _hash_embedding(text, dim=384):
    """Deterministic fallback embedding — keyword-search quality."""
    vec = [0.0] * dim
    for token in text.lower().split():
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def embed_texts(texts):
    if not texts:
        return []

    model = _load_model()
    if model is not None:
        try:
            vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
            return [v.tolist() for v in vecs]
        except Exception as e:
            print("[embeddings] ST encode failed:", e)

    # HuggingFace fallback
    hf_token = os.environ.get("HF_TOKEN", "")
    if hf_token:
        import requests
        url = ("https://api-inference.huggingface.co/pipeline/feature-extraction/"
               + _MODEL_NAME)
        try:
            r = requests.post(
                url,
                headers={"Authorization": "Bearer " + hf_token},
                json={"inputs": texts, "options": {"wait_for_model": True}},
                timeout=60,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass

    # Last resort
    return [_hash_embedding(t) for t in texts]


def embed_one(text):
    out = embed_texts([text])
    return out[0] if out else None
