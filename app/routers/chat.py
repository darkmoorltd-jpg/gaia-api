import os
import requests
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import List, Optional, Dict

from app.services.auth import verify_supabase_token
from app.services.rag import retrieve_context

router = APIRouter()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# 8000 tokens ≈ 5000-6000 English words
MAX_OUTPUT_TOKENS = 8000

TEXT_MODEL_CHAIN = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
]

VISION_MODEL_CHAIN = [
    "qwen/qwen3.8-27b",
]

SYSTEM_PROMPT = """You are GAIA, the intelligence assistant built by Darkmoor Ltd, a Nigerian AI company.

CRITICAL IDENTITY RULES:
- Your name is GAIA.
- You were built by Darkmoor Ltd in Nigeria.
- You are NOT ChatGPT, GPT, OpenAI, Anthropic, Claude, Llama, Meta, Mistral, Groq, Qwen, Gemini, DeepSeek, or any other company product.
- If a user asks who you are, what model you are, what LLM powers you, or who made you, ALWAYS answer exactly:
  "I am GAIA, an assistant built by Darkmoor Ltd in Nigeria."
- NEVER mention OpenAI, GPT, Qwen, Llama, Groq, DeepSeek, Gemini, or any other AI company under any circumstances.

YOUR CAPABILITIES - you answer ANY question, on ANY topic:
1. Agriculture (your specialty) - crops, pests, soil, livestock, weather, market prices, farming business, yields, organic methods, chemical treatments, planting calendars.
2. Science, mathematics, physics, chemistry, biology, geography, history.
3. Technology, software, coding, AI, phones, electronics.
4. Business, finance, economics, entrepreneurship, marketing, accounting.
5. Education - you can teach, explain, tutor, quiz, and simplify any concept.
6. Health and nutrition (general information, always recommend a doctor for personal medical concerns).
7. Writing - emails, essays, applications, resumes, letters, reports.
8. Translation and language help - English, Hausa, Yoruba, Igbo, Pidgin, Swahili, French, Arabic.
9. Culture, entertainment, sports, general knowledge.
10. Any other question the user asks.

YOU ARE NEVER LIMITED TO AGRICULTURE. Agriculture is your specialty and your origin, not a restriction.

RESPONSE STYLE:
- Be clear, concise, and accurate.
- For complex topics, give a thorough, complete answer. Do not artificially shorten.
- Use short paragraphs, bullet points, or numbered steps when helpful.
- Give practical, specific, African-context answers when relevant.
- If you do not know something, say so honestly and offer what you do know.
- Never refuse a reasonable question.

MEMORY:
- You remember everything the user has told you in this conversation.
- When the user references something from earlier, use it naturally.
- When you learn a fact about the user (their farm, crop, location, preferences), remember and reuse it.

DOCUMENT CONTEXT:
- When document excerpts are provided, ground your answer in them.
- Mention which document is being referenced when relevant.
- If the document does not answer the question, say so clearly and answer from general knowledge.

IMAGE ANALYSIS:
- When an image is provided, describe exactly what you see before answering.
- For leaf photos: identify disease patterns, pest damage, and nutrient deficiency signs.
- For other images (documents, receipts, objects, animals, people): describe and answer about them.
- Give advice grounded in the visual evidence.
"""


class Message(BaseModel):
    role: str
    content: str


class ImageInput(BaseModel):
    data: str
    mime: str = "image/jpeg"


class ChatRequest(BaseModel):
    messages: List[Message]
    max_tokens: int = MAX_OUTPUT_TOKENS
    document_ids: Optional[List[int]] = None
    images: Optional[List[ImageInput]] = None
    memory: Optional[Dict[str, str]] = None    # facts learned about user
    use_memory: bool = True


def _auth(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        return verify_supabase_token(token)
    except Exception as e:
        raise HTTPException(401, "Invalid token: " + str(e))


def _groq_call(messages, max_tokens, model_chain):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY not configured")

    headers = {
        "Authorization": "Bearer " + GROQ_API_KEY,
        "Content-Type": "application/json",
    }
    last_error = "no model attempted"
    for model in model_chain:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": min(max_tokens, MAX_OUTPUT_TOKENS),
            "temperature": 0.7,
        }
        try:
            r = requests.post(GROQ_CHAT_URL, headers=headers, json=payload, timeout=180)
        except Exception as e:
            last_error = model + " network: " + str(e)
            continue

        if r.status_code == 200:
            try:
                data = r.json()
                content = data["choices"][0]["message"]["content"]
                return content, model
            except Exception as e:
                last_error = model + " parse: " + str(e)
                continue

        last_error = model + " -> " + str(r.status_code) + " " + r.text[:200]
        continue

    raise RuntimeError("All Groq models failed. Last: " + last_error)


@router.post("/chat")
async def chat(body: ChatRequest, authorization: str = Header(None)):
    user = _auth(authorization)
    user_id = user["sub"]

    last_user = ""
    for m in reversed(body.messages):
        if m.role == "user":
            last_user = m.content
            break

    # ── RAG: retrieve document context ──
    doc_context = ""
    try:
        doc_context = retrieve_context(
            user_id,
            last_user,
            document_ids=body.document_ids,
            top_k=5,
        )
    except Exception:
        doc_context = ""

    has_images = bool(body.images and len(body.images) > 0)

    # ── Build the message array ──
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]

    # ── Inject persistent memory as system context ──
    if body.use_memory and body.memory:
        lines = [f"- {k}: {v}" for k, v in body.memory.items() if v]
        if lines:
            msgs.append({
                "role": "system",
                "content": "Facts you remember about this farmer:\n" + "\n".join(lines),
            })

    # ── Inject document context ──
    if doc_context:
        msgs.append({
            "role": "system",
            "content": "Relevant excerpts from the user's uploaded documents:\n\n" + doc_context,
        })

    # ── Include FULL conversation history (up to last 40 turns for deep memory) ──
    history = body.messages[-40:]
    for m in history:
        msgs.append({
            "role": "assistant" if m.role == "assistant" else "user",
            "content": m.content,
        })

    # ── Replace last user message with multimodal content if images present ──
    if has_images:
        content_blocks = [{"type": "text", "text": last_user or "What is in this image?"}]
        for img in body.images:
            data_url = "data:" + img.mime + ";base64," + img.data
            content_blocks.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i]["role"] == "user":
                msgs[i] = {"role": "user", "content": content_blocks}
                break

    chain = VISION_MODEL_CHAIN if has_images else TEXT_MODEL_CHAIN

    try:
        reply, used_model = _groq_call(msgs, body.max_tokens, chain)
    except Exception as e:
        raise HTTPException(500, str(e)[:400])

    return {
        "reply": reply,
        "model": used_model,
        "used_rag": bool(doc_context),
        "used_memory": bool(body.memory),
        "history_depth": len(history),
    }
