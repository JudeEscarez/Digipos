"""
AI review reader: for each lead, an AI model reads the review excerpts copied from its
public listing and answers a few questions (cash only? already takes cards? business type?
possibly restricted?). The answers are saved with the lead, and the normal scoring rules
use them: cash only +5, cards accepted -25, restricted -> "check with bank" flag only.

Run it ONCE before the demo (results are saved, so the demo never calls the AI live):

    pip install anthropic
    export ANTHROPIC_API_KEY=...                 # never commit this
    python scripts/ai_review_reader.py --test     # 2 built-in sanity checks
    python scripts/ai_review_reader.py            # read every lead that has excerpts, save, re-score
    python scripts/ai_review_reader.py --eval data/human_labels.csv   # agreement with a human reader

Where results go:
  - data/ai_results.json (always; also picked up by scripts/import_xlsx.py for the offline copy)
  - Supabase, if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set (skip with --no-push)

Honesty rules enforced here, not just asked of the model:
  - answers other than yes/no/unclear become "unclear"
  - a "yes" on cash_only / cards_accepted must come with evidence copied word for word from
    the excerpts; if the quote is not found, both answers fall back to "unclear"
  - any error (network, bad JSON) gives an all-"unclear" result, so no score changes
"""

import argparse
import csv
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("api_core", os.path.join(ROOT, "api", "index.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)

MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")  # confirm the current model ID in Anthropic's docs
PROMPT_PATH = os.path.join(ROOT, "prompts", "review_reader.txt")
RESULTS_PATH = os.path.join(ROOT, "data", "ai_results.json")
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


def call_model(system, user):
    """One API call. Kept separate so tests can swap it out."""
    import anthropic  # pip install anthropic
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(model=MODEL, max_tokens=400, temperature=0, system=system,
                                 messages=[{"role": "user", "content": user}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
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
    import anthropic
    client = anthropic.Anthropic()
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
        resp = client.messages.create(model=MODEL, max_tokens=2000, system=system, tools=tools, messages=messages)
        content = [b.model_dump() for b in resp.content]
        blocks.extend(content)
        u = resp.usage
        usage["input_tokens"] += u.input_tokens or 0
        usage["output_tokens"] += u.output_tokens or 0
        stu = getattr(u, "server_tool_use", None)
        usage["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        if resp.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": user}, {"role": "assistant", "content": resp.content}]
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


def run_tests(system, caller=None):
    cases = [
        ("cash only please, no card machine", {"cash_only": "yes"}),
        ("Lovely coffee and friendly staff, great view of the park.", {"cash_only": "unclear", "cards_accepted": "unclear"}),
        ("Paid by card, no problem at all. Great service.", {"cards_accepted": "yes"}),
    ]
    ok = 0
    for text, expect in cases:
        lead = {"business_name": "Test Cafe", "category": "Cafe", "area": "Al Barsha", "emirate": "Dubai",
                "rating": 4.5, "review_count": 100, "review_excerpts": [{"text": text}]}
        got = read_reviews(lead, system, caller)
        passed = all(got.get(k) == v for k, v in expect.items())
        ok += passed
        print(f"{'PASS' if passed else 'FAIL'}  {text!r}\n      expected {expect}\n      got {{cash_only: {got['cash_only']}, cards_accepted: {got['cards_accepted']}, evidence: {got['evidence']!r}}}"
              + (f"\n      error: {got['error']}" if got.get("error") else ""))
    print(f"\n{ok}/{len(cases)} tests passed")
    return ok == len(cases)


def run_eval(path, results):
    """Compare saved AI answers with a human reader's answers (CSV: lead_id,cash_only,cards_accepted)."""
    agree = total = 0
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ai = results.get(row["lead_id"])
            if not ai:
                continue
            total += 1
            same = all(ai.get(k) == (row.get(k) or "").strip().lower() for k in ("cash_only", "cards_accepted"))
            agree += same
            if not same:
                print(f"DIFF {row['lead_id']}: human {row.get('cash_only')}/{row.get('cards_accepted')}  "
                      f"AI {ai.get('cash_only')}/{ai.get('cards_accepted')}  evidence {ai.get('evidence')!r}")
    print(f"\nThe AI agreed with a human reader on {agree} of {total} businesses.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run built-in sanity checks only")
    ap.add_argument("--eval", help="CSV of human answers to compare with saved results")
    ap.add_argument("--only", help="comma-separated lead IDs, e.g. L001,L009")
    ap.add_argument("--no-push", action="store_true", help="save to data/ai_results.json only")
    ap.add_argument("--from-seed", action="store_true", help="read leads from data/seed.json instead of the database")
    ap.add_argument("--web", action="store_true",
                    help="let the AI search the web and read sources itself (no review excerpts needed)")
    ap.add_argument("--skip-done", action="store_true", help="skip leads that already have a saved result")
    a = ap.parse_args()

    system = open(WEB_PROMPT_PATH if a.web else PROMPT_PATH).read()
    results = json.load(open(RESULTS_PATH)) if os.path.exists(RESULTS_PATH) else {}

    if a.test:
        sys.exit(0 if run_tests(open(PROMPT_PATH).read()) else 1)
    if a.eval:
        return run_eval(a.eval, results)

    store = core.get_store()
    use_db = isinstance(store, core.SupabaseStore) and not a.from_seed
    leads = store.leads() if use_db else json.load(open(os.path.join(ROOT, "data", "seed.json")))["leads"]
    only = set(a.only.split(",")) if a.only else None
    if a.web:
        todo = [l for l in leads if not l.get("is_example") and (not only or l["id"] in only)]
    else:
        todo = [l for l in leads if l.get("review_excerpts") and (not only or l["id"] in only)]
    if a.skip_done:
        todo = [l for l in todo if l["id"] not in results]
    if not todo:
        sys.exit("Nothing to do. Without --web, fill the 'Review excerpts' sheet and re-run scripts/import_xlsx.py first.")

    totals = {"input_tokens": 0, "output_tokens": 0, "searches": 0}
    for n, l in enumerate(todo, 1):
        if a.web:
            out, usage = research_web(l, system)
            for k in totals:
                totals[k] += usage.get(k, 0)
        else:
            out = read_reviews(l, system)
        results[l["id"]] = out
        # save as we go, so a stopped run keeps what it has (re-run with --skip-done)
        with open(RESULTS_PATH, "w") as f:
            json.dump(results, f, indent=1, ensure_ascii=False)
        flag = " CHECK-BANK" if out["restricted_check"] == "yes" else ""
        print(f"[{n}/{len(todo)}] {l['id']} {l['business_name'][:30]:30} cash_only={out['cash_only']:8} "
              f"cards={out['cards_accepted']:8} type={out['business_type']:10}{flag}  "
              f"{out.get('error') or out.get('note') or out['evidence']!r}")
    if a.web:
        print(f"\nWeb searches: {totals['searches']} · tokens in/out: {totals['input_tokens']:,}/{totals['output_tokens']:,}")

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(results, f, indent=1, ensure_ascii=False)
    print(f"\nSaved {len(todo)} results to data/ai_results.json")

    if use_db and not a.no_push:
        store.upsert_leads([{"id": i, "ai": results[i]} for i in (l["id"] for l in todo)])
        fresh = store.leads()
        store.upsert_leads(core.rescore(fresh, store.visits()))  # scores now use the AI answers
        print("Saved to Supabase and re-scored.")
    elif not use_db:
        print("Not pushed (no Supabase keys). Run scripts/import_xlsx.py to include results in the offline copy.")


if __name__ == "__main__":
    main()
