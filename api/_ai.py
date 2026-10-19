"""
Shared AI review-check logic, used by the website (api/index.py, POST /api/ai-research)
and by scripts/ai_review_reader.py. Standard library only.

Two modes:
  - excerpts: the model reads review excerpts someone copied in (read_reviews)
  - web:      the model searches the web and opens pages itself (research_web)
Both return the same answer shape, saved on the lead as `ai` and used by the scoring.
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")

MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")  # confirm the current model ID in Anthropic's docs
PROMPT_PATH = os.path.join(ROOT, "prompts", "review_reader.txt")
PROMPT_VERSION = "review_reader v1"

ALLOWED = {"yes", "no", "unclear"}
TYPES = {"cafe", "restaurant", "salon", "barber", "clinic", "dental", "grocery", "retail", "gym", "laundry", "other"}
UNCLEAR = {"cash_only": "unclear", "cards_accepted": "unclear", "business_type": "other",
           "restricted_check": "unclear", "evidence": "", "reason": ""}


def _norm(s):
    s = (s or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip(" .,'\"")


def build_user_message(lead):
    excerpts = [e.get("text", "").strip() for e in (lead.get("review_excerpts") or []) if e.get("text", "").strip()]
    lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(excerpts))
    rating = f"{lead.get('rating')} from {lead.get('review_count')} reviews" if lead.get("rating") else "not listed"
    return (f"Business: {lead.get('business_name')}\n"
            f"Category on listing: {lead.get('category')}\n"
            f"Area: {lead.get('area')}, {lead.get('emirate')}\n"
            f"Rating: {rating}\n\n"
            f"<reviews>\n{lines}\n</reviews>"), excerpts


def validate(raw, excerpts):
    """Clean the model's answer and enforce the honesty rules."""
    out = dict(UNCLEAR)
    if isinstance(raw, dict):
        out.update({k: raw.get(k, out[k]) for k in out})
    for k in ("cash_only", "cards_accepted", "restricted_check"):
        if out[k] not in ALLOWED:
            out[k] = "unclear"
    if out["business_type"] not in TYPES:
        out["business_type"] = "other"
    out["evidence"] = str(out["evidence"] or "").strip()
    out["reason"] = str(out["reason"] or "").strip()
    if len(out["reason"].split()) > 30:
        out["reason"] = ""
    # A "yes" on payments must be backed by words that really appear in the excerpts.
    if "yes" in (out["cash_only"], out["cards_accepted"]):
        found = out["evidence"] and any(_norm(out["evidence"]) in _norm(t) for t in excerpts)
        if not found:
            for k in ("cash_only", "cards_accepted"):
                if out[k] == "yes":
                    out[k] = "unclear"
            out["evidence"] = ""
            out["note"] = "evidence not found in excerpts, answer set to unclear"
    else:
        out["evidence"] = ""
    return out


def _post_messages(payload, timeout=120):
    """POST /v1/messages with only the standard library (works on Vercel without extra packages)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    req = urllib.request.Request(API_BASE + "/v1/messages", data=json.dumps(payload).encode(), method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Anthropic API {e.code}: {e.read().decode()[:300]}")


def call_model(system, user):
    """One plain call (excerpt mode). Kept separate so tests can swap it out."""
    resp = _post_messages({"model": MODEL, "max_tokens": 400, "temperature": 0, "system": system,
                           "messages": [{"role": "user", "content": user}]})
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # tolerate a stray code fence
    return json.loads(text)


def read_reviews(lead, system, caller=None):
    caller = caller or call_model
    user, excerpts = build_user_message(lead)
    try:
        raw = caller(system, user)
        out = validate(raw, excerpts)
    except Exception as e:  # network, auth, bad JSON: safe fallback, no score change
        out = dict(UNCLEAR, error=f"{type(e).__name__}: {e}"[:200])
    out.update(reviews_read=len(excerpts), model=MODEL, prompt=PROMPT_VERSION,
               run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out


# ---------------------------------------------------------------------------
# Web mode: the model finds and reads sources itself (no spreadsheet needed)
# ---------------------------------------------------------------------------

WEB_PROMPT_PATH = os.path.join(ROOT, "prompts", "web_researcher.txt")
WEB_PROMPT_VERSION = "web_researcher v1"
MAX_SEARCHES, MAX_FETCHES = 3, 3
GENERIC = {"cafe", "café", "coffee", "restaurant", "salon", "ladies", "beauty", "dental", "clinic", "center",
           "centre", "the", "and", "gents", "men's", "mens", "barber", "barbershop", "lounge", "kitchen",
           "bakery", "mall", "branch", "concept", "store", "specialty", "roastery", "house"}
EMIRATE_CITY = {"Dubai": "Dubai", "Abu Dhabi": "Abu Dhabi", "Sharjah": "Sharjah", "Ajman": "Ajman",
                "Umm Al Quwain": "Umm Al Quwain", "Ras Al Khaimah": "Ras Al Khaimah", "Fujairah": "Fujairah"}


def web_user_message(lead):
    lines = [f"Business: {lead.get('business_name')}",
             f"Category on listing: {lead.get('category')}",
             f"Location: {lead.get('area')}, {lead.get('emirate')}, United Arab Emirates"]
    if lead.get("website"):
        lines.append(f"Website or social page: {lead['website']}")
    return "\n".join(lines)


def call_model_web(system, user, emirate=None):
    """Runs the research turn with server-side web_search + web_fetch. Returns all content blocks as dicts."""
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES,
         "user_location": {"type": "approximate", "country": "AE", "timezone": "Asia/Dubai",
                           **({"city": EMIRATE_CITY[emirate]} if emirate in EMIRATE_CITY else {})}},
        {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": MAX_FETCHES,
         "citations": {"enabled": True}, "max_content_tokens": 8000},
    ]
    messages = [{"role": "user", "content": user}]
    blocks, usage = [], {"input_tokens": 0, "output_tokens": 0, "searches": 0}
    for _ in range(4):  # handle pause_turn: resend the paused assistant turn unchanged
        resp = _post_messages({"model": MODEL, "max_tokens": 2000, "system": system, "tools": tools,
                               "messages": messages}, timeout=240)
        content = resp.get("content", [])
        blocks.extend(content)
        u = resp.get("usage") or {}
        usage["input_tokens"] += u.get("input_tokens") or 0
        usage["output_tokens"] += u.get("output_tokens") or 0
        usage["searches"] += ((u.get("server_tool_use") or {}).get("web_search_requests") or 0)
        if resp.get("stop_reason") != "pause_turn":
            break
        messages = [{"role": "user", "content": user}, {"role": "assistant", "content": content}]
    return blocks, usage


def _evidence_trail(blocks):
    """What the model actually opened: search-result URLs, fetched page texts, and cited passages."""
    seen, pages, cited = set(), {}, []
    for b in blocks:
        t = b.get("type")
        if t == "web_search_tool_result" and isinstance(b.get("content"), list):
            for r in b["content"]:
                if r.get("url"):
                    seen.add(r["url"])
        elif t == "web_fetch_tool_result" and isinstance(b.get("content"), dict):
            c = b["content"]
            if c.get("type") == "web_fetch_result" and c.get("url"):
                seen.add(c["url"])
                src = (c.get("content") or {}).get("source") or {}
                if src.get("type") == "text":
                    pages[c["url"]] = (c.get("content") or {}).get("title", "") + "\n" + (src.get("data") or "")
        elif t == "text":
            for cit in b.get("citations") or []:
                if cit.get("url") or cit.get("cited_text"):
                    cited.append((cit.get("url"), (cit.get("title") or "") + "\n" + (cit.get("cited_text") or "")))
    return seen, pages, cited


def _final_json(blocks):
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    joined = "".join(texts)
    i, j = joined.rfind("{\"cash_only\""), joined.rfind("}")
    if i < 0:
        i = joined.find("{")
    return json.loads(joined[i:j + 1])


def _name_tokens(name):
    words = re.findall(r"[a-z0-9']+", (name or "").lower())
    return [w for w in words if len(w) >= 3 and w not in GENERIC]


def validate_web(raw, blocks, lead):
    out = dict(UNCLEAR, evidence_url="", sources=[])
    if isinstance(raw, dict):
        out.update({k: raw.get(k, out[k]) for k in out})
    for k in ("cash_only", "cards_accepted", "restricted_check"):
        if out[k] not in ALLOWED:
            out[k] = "unclear"
    if out["business_type"] not in TYPES:
        out["business_type"] = "other"
    out["reason"] = str(out["reason"] or "").strip()
    if len(out["reason"].split()) > 30:
        out["reason"] = ""
    seen, pages, cited = _evidence_trail(blocks)
    out["sources"] = [u for u in (out.get("sources") or []) if isinstance(u, str) and u in seen][:3]
    out["sources_opened"] = len(seen)
    if "yes" in (out["cash_only"], out["cards_accepted"]):
        ev, url = _norm(str(out["evidence"] or "")), str(out["evidence_url"] or "")
        texts = ([pages[url]] if url in pages else []) + [t for u, t in cited if u == url]
        quote_ok = bool(ev) and url in seen and any(ev in _norm(t) for t in texts)
        tokens = _name_tokens(lead.get("business_name"))
        name_ok = not tokens or any(any(tok in _norm(t) for tok in tokens) for t in texts)
        if not (quote_ok and name_ok):
            for k in ("cash_only", "cards_accepted"):
                if out[k] == "yes":
                    out[k] = "unclear"
            out["note"] = ("evidence quote not found on a page the AI opened" if not quote_ok
                           else "evidence page does not name this business")
            out["evidence"], out["evidence_url"] = "", ""
        elif url not in out["sources"]:
            out["sources"] = [url] + out["sources"][:2]
    else:
        out["evidence"], out["evidence_url"] = "", ""
    return out


def research_web(lead, system, caller=None):
    caller = caller or call_model_web
    usage = {}
    try:
        blocks, usage = caller(system, web_user_message(lead), lead.get("emirate"))
        try:
            raw = _final_json(blocks)
        except Exception:
            raw = None
        out = validate_web(raw, blocks, lead)
        if raw is None:
            out["error"] = "no JSON answer in the final message"
    except Exception as e:
        out = dict(UNCLEAR, evidence_url="", sources=[], error=f"{type(e).__name__}: {e}"[:200])
    out.update(mode="web", reviews_read=len(out.get("sources") or []), model=MODEL, prompt=WEB_PROMPT_VERSION,
               run_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out, usage
