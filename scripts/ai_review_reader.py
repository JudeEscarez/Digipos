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
    a = ap.parse_args()

    system = open(PROMPT_PATH).read()
    results = json.load(open(RESULTS_PATH)) if os.path.exists(RESULTS_PATH) else {}

    if a.test:
        sys.exit(0 if run_tests(system) else 1)
    if a.eval:
        return run_eval(a.eval, results)

    store = core.get_store()
    use_db = isinstance(store, core.SupabaseStore) and not a.from_seed
    leads = store.leads() if use_db else json.load(open(os.path.join(ROOT, "data", "seed.json")))["leads"]
    only = set(a.only.split(",")) if a.only else None
    todo = [l for l in leads if l.get("review_excerpts") and (not only or l["id"] in only)]
    if not todo:
        sys.exit("No leads have review excerpts yet. Fill the 'Review excerpts' sheet and re-run scripts/import_xlsx.py first.")

    for l in todo:
        out = read_reviews(l, system)
        results[l["id"]] = out
        flag = " CHECK-BANK" if out["restricted_check"] == "yes" else ""
        print(f"{l['id']} {l['business_name'][:32]:32} cash_only={out['cash_only']:8} cards={out['cards_accepted']:8}"
              f" type={out['business_type']:10}{flag}  {out.get('error') or out['evidence']!r}")

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
