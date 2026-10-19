"""
AI review reader: for each lead, an AI model reads the review excerpts copied from its
public listing and answers a few questions (cash only? already takes cards? business type?
possibly restricted?). The answers are saved with the lead, and the normal scoring rules
use them: cash only +5, cards accepted -25, restricted -> "check with bank" flag only.

Run it ONCE before the demo (results are saved, so the demo never calls the AI live):

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

# Load keys from .env (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ANTHROPIC_API_KEY) if present.
_env = os.path.join(ROOT, ".env")
if os.path.exists(_env):
    for _line in open(_env):
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.strip().split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"'))
spec = importlib.util.spec_from_file_location("api_core", os.path.join(ROOT, "api", "index.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
sys.path.insert(0, os.path.join(ROOT, "api"))
from _ai import *  # noqa: E402,F401,F403  (shared AI logic)
from _ai import _norm  # noqa: E402,F401
RESULTS_PATH = os.path.join(ROOT, "data", "ai_results.json")



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
