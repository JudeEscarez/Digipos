"""
Digipos demo API — one Vercel Python function serving every /api route.

Routes
  GET  /api/leads?emirate=Dubai&lane=established   ranked leads
  POST /api/outcome {"lead_id": "L009", "outcome": "has_terminal"}
  GET  /api/health                                 merchant health list
  POST /api/reset                                  clear visits, re-score (for rehearsals)

Storage
  If SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are set, data lives in Supabase.
  Otherwise it uses data/local_db.json (offline laptop fallback, see dev_server.py).

No third-party packages: only the standard library, so there is nothing to install.
"""

import json
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Scoring config — every number here is an assumption. Change freely.
# ---------------------------------------------------------------------------

WEIGHTS = {"newness": 30, "size": 25, "category": 20, "location": 15, "contact": 10}

AS_OF = date(2026, 10, 17)  # demo "today"; keeps scores stable between rehearsals

CATEGORY_FIT = {  # out of 20: how well card terminals suit the business type
    "cafe": 20, "restaurant": 20, "cafe / restaurant": 20, "bakery": 19,
    "supermarket": 18, "grocery": 18, "retail shop": 17, "retail": 17,
    "salon": 17, "barber": 16, "dental clinic": 15, "clinic": 15,
    "gym": 14, "laundry": 14, "car wash": 14,
}
CATEGORY_FIT_DEFAULT = 12

# Our assumption only — replace with the acquiring bank's prohibited list.
RESTRICTED_CATEGORIES = {"shisha", "shisha lounge", "tobacco", "vape shop",
                         "money exchange", "gold trading", "nightclub"}

AREA_TIER = {  # out of 15 before coverage penalty: footfall / card-usage proxy
    "Downtown Dubai": 15, "Business Bay": 15, "JLT": 15, "Trade Centre": 14,
    "Deira": 14, "Karama": 14, "Satwa": 13, "Al Barsha": 13, "Jumeirah": 13, "Al Quoz": 12,
    "Al Zahiyah": 14, "Al Reem Island": 14, "Khalidiyah": 13, "Al Ain": 11,
    "Aljada": 13, "Al Khan": 12, "Al Nuaimiya": 12, "Al Hamra": 11,
}
AREA_TIER_DEFAULT = 11

COVERAGE_PENALTY = 6      # location points lost per "already has a terminal" in the same area
COVERAGE_PENALTY_CAP = 15

# Area names are free text in the sheet; map them onto one neighbourhood key.
CLUSTER_ALIASES = {
    "al nuaimia": "Al Nuaimiya", "al nuaimiya": "Al Nuaimiya",
    "al jimi": "Al Ain", "al ain": "Al Ain",
    "umm suqeim": "Jumeirah", "jumeirah": "Jumeirah",
    "one central": "Trade Centre", "trade centre": "Trade Centre",
    "al karama": "Karama", "karama": "Karama", "al satwa": "Satwa", "satwa": "Satwa",
}

STATUS_FOR_OUTCOME = {"interested": "interested", "not_now": "not_now",
                      "has_terminal": "has_terminal", "signed": "signed"}
CLOSED_STATUSES = {"has_terminal", "signed"}


def cluster_for(area, emirate=""):
    """'Al Rigga, Deira' -> 'Deira', 'Al Quoz 4' -> 'Al Quoz', 'Jumeirah 2' -> 'Jumeirah'."""
    if not area:
        return emirate or "Unknown"
    parts = [p.strip() for p in str(area).split(",") if p.strip()]
    # Prefer any part that is a known alias, else the last part.
    for p in parts:
        key = re.sub(r"\s*\d+$", "", p).strip().lower()
        if key in CLUSTER_ALIASES:
            return CLUSTER_ALIASES[key]
    last = re.sub(r"\s*\d+$", "", parts[-1]).strip()
    return CLUSTER_ALIASES.get(last.lower(), last)


def _to_date(v):
    if not v:
        return None
    if isinstance(v, date):
        return v if not isinstance(v, datetime) else v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def score_lead(lead, coverage):
    """Return (score 0-100, parts dict, reason str). coverage = {cluster: n has_terminal}."""
    cat = (lead.get("category") or "").strip().lower()
    cluster = lead.get("cluster") or cluster_for(lead.get("area"), lead.get("emirate"))
    is_new = lead.get("lane") == "new_opening"
    notes = []

    # Newness (30)
    opened = _to_date(lead.get("licence_issue_date")) or _to_date(lead.get("oldest_review_date"))
    if opened:
        months = max(0, (AS_OF - opened).days / 30.4)
        newness = 30 if months <= 3 else 24 if months <= 6 else 16 if months <= 12 else 8 if months <= 36 else 4
        m = int(round(months))
        newness_note = ("opened in the last month" if m < 1 else
                        f"opened about {m} month{'s' if m > 1 else ''} ago")
    elif is_new:
        newness, newness_note = 20, "possible new opening (unverified)"
    else:
        newness, newness_note = 8, "opening date not checked yet"

    # Size proxy (25)
    if is_new:
        size = round(CATEGORY_FIT.get(cat, CATEGORY_FIT_DEFAULT) * 0.6 + AREA_TIER.get(cluster, AREA_TIER_DEFAULT) * 0.4)
        size_note = "size estimated from category and area"
    else:
        rc = lead.get("review_count") or 0
        reviews_pts = min(1.0, math.log10(rc + 1) / math.log10(3000)) * 17
        price_pts = {1: 5, 2: 8, 3: 8, 4: 6}.get(lead.get("price_level"), 4)
        size = round(reviews_pts + price_pts)
        size_note = f"{rc:,} reviews" if rc else "few reviews"
    size = min(size, 25)

    # Category fit (20)
    category = CATEGORY_FIT.get(cat, CATEGORY_FIT_DEFAULT)
    chain = (lead.get("chain_check") or "").lower()
    if "developer" in chain:
        category -= 5
        notes.append("developer-run, decision may sit with head office")
    elif "group" in chain:
        category -= 3
        notes.append("small group, confirm who decides")
    category = max(category, 0)

    # Location (15) minus coverage learned from visits
    base_loc = AREA_TIER.get(cluster, AREA_TIER_DEFAULT)
    found = coverage.get(cluster, 0)
    penalty = min(COVERAGE_PENALTY * found, COVERAGE_PENALTY_CAP)
    location = max(base_loc - penalty, 0)
    if found:
        notes.append(f"{found} nearby business{'es' if found > 1 else ''} in {cluster} already "
                     f"{'have' if found > 1 else 'has'} a terminal")

    # Contactability (10)
    contact = (6 if lead.get("has_phone") else 0) + (4 if lead.get("website") else 0)

    parts = {"newness": newness, "size": size, "category": category,
             "location": location, "contact": contact}
    total = sum(parts.values())

    # Signals outside the 100 points
    signal = (lead.get("card_signal") or "").lower()
    if "cash" in signal:
        total += 5
        parts["signal"] = 5
        notes.insert(0, "reviews mention cash only")
    elif "served" in signal:
        total -= 25
        parts["signal"] = -25
        notes.insert(0, "reviews suggest cards already accepted")

    restricted = bool(lead.get("restricted")) or cat in RESTRICTED_CATEGORIES
    if restricted:
        total = 0
        parts["restricted"] = True

    total = max(0, min(100, int(round(total))))

    # Reason: the two strongest factors (as a share of their weight) + caveats.
    if restricted:
        reason = "Restricted category: check with the acquiring bank before visiting."
    else:
        labels = {
            "newness": newness_note,
            "size": size_note,
            "category": f"{lead.get('category') or 'business'} is a strong fit for card terminals",
            "location": f"busy area ({cluster})",
            "contact": "easy to contact" if contact >= 10 else "reachable by phone",
        }
        ranked = sorted(WEIGHTS, key=lambda k: parts[k] / WEIGHTS[k], reverse=True)
        top = [labels[k] for k in ranked[:2] if parts[k] / WEIGHTS[k] >= 0.6]
        reason = "; ".join(top or [labels[ranked[0]]])
        reason = reason[0].upper() + reason[1:]
        if notes:
            reason += ". Note: " + "; ".join(notes)
        reason += "."
    return total, parts, reason


def draft_message(lead):
    """Pre-written WhatsApp opener (stored with the lead so the demo never waits on AI)."""
    name = (lead.get("business_name") or "there").replace("Example: ", "")
    cat = (lead.get("category") or "").lower()
    area = lead.get("area") or lead.get("emirate") or ""
    hooks = {
        "cafe": "tap-to-pay keeps the morning queue moving",
        "restaurant": "guests can split the bill and pay at the table",
        "salon": "clients can pay by card or Apple Pay right at the chair",
        "barber": "customers can tap to pay in a few seconds",
        "dental clinic": "patients can settle co-payments by card at reception",
        "clinic": "patients can settle co-payments by card at reception",
        "supermarket": "a second counter terminal helps at peak hours",
    }
    hook = hooks.get(cat) or hooks.get(cat.split(" /")[0]) or "customers can pay by card, Apple Pay or Google Pay"
    opener = (f"Congratulations on opening in {area}!" if lead.get("lane") == "new_opening"
              else f"I came across {name} in {area} and loved the reviews.")
    return (f"Hi {name} team, this is [rep name] from Digipos. {opener} "
            f"We set up card payment terminals for local businesses, so {hook}. "
            f"Could I drop by for 10 minutes this week to show you? Thank you!")


# ---------------------------------------------------------------------------
# Merchant health (same rule as the workbook formulas)
# ---------------------------------------------------------------------------

def merchant_health(m):
    jun, sep = m.get("vol_jun"), m.get("vol_sep")
    if not jun:
        change, status = None, "New (ramp-up)"
    else:
        change = (sep - jun) / jun if sep is not None else None
        status = ("At risk" if change is not None and change <= -0.25
                  else "Watch" if change is not None and change <= -0.10 else "Healthy")
    action = {"At risk": "Call this week", "Watch": "Courtesy visit",
              "New (ramp-up)": "Day 30 check-in"}.get(status, "Suggest payment links")
    out = dict(m)
    out.update(change_jun_to_sep=change, status=status, suggested_action=action)
    return out


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class SupabaseStore:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {"apikey": key, "Content-Type": "application/json"}
        if key.startswith("eyJ"):  # legacy JWT service_role key
            self.headers["Authorization"] = "Bearer " + key

    def _req(self, method, path, body=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Supabase {e.code}: {e.read().decode()[:300]}")

    def leads(self):
        return self._req("GET", "leads?select=*")

    def merchants(self):
        return self._req("GET", "merchants?select=*&order=id")

    def visits(self):
        return self._req("GET", "visits?select=*&order=created_at")

    def add_visit(self, lead_id, outcome, note=None):
        self._req("POST", "visits", {"lead_id": lead_id, "outcome": outcome, "note": note})

    def clear_visits(self):
        self._req("DELETE", "visits?id=gte.0")

    def upsert_leads(self, rows):
        """Update existing leads with partial rows (score fields). PATCH per row, in parallel,
        because a bulk upsert would trip NOT NULL checks on columns we don't send."""
        from concurrent.futures import ThreadPoolExecutor

        def patch(r):
            body = {k: v for k, v in r.items() if k != "id"}
            self._req("PATCH", "leads?id=eq." + urllib.parse.quote(r["id"]), body,
                      prefer="return=minimal")

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(patch, rows))

    def insert_or_replace(self, table, rows):
        if rows:
            self._req("POST", f"{table}?on_conflict=id", rows,
                      prefer="resolution=merge-duplicates,return=minimal")


class LocalStore:
    def __init__(self, path):
        self.path = path
        if not os.path.exists(path):
            seed = os.path.join(ROOT, "data", "seed.json")
            with open(seed) as f:
                db = json.load(f)
            db.setdefault("visits", [])
            self._save(db)

    def _load(self):
        with open(self.path) as f:
            return json.load(f)

    def _save(self, db):
        with open(self.path, "w") as f:
            json.dump(db, f, indent=1, default=str)

    def leads(self):
        return self._load()["leads"]

    def merchants(self):
        return self._load()["merchants"]

    def visits(self):
        return self._load()["visits"]

    def add_visit(self, lead_id, outcome, note=None):
        db = self._load()
        db["visits"].append({"lead_id": lead_id, "outcome": outcome, "note": note,
                             "created_at": datetime.utcnow().isoformat()})
        self._save(db)

    def clear_visits(self):
        db = self._load()
        db["visits"] = []
        self._save(db)

    def upsert_leads(self, rows):
        db = self._load()
        by_id = {l["id"]: l for l in db["leads"]}
        for r in rows:
            by_id.setdefault(r["id"], {}).update(r)
        db["leads"] = list(by_id.values())
        self._save(db)


def get_store():
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if url and key:
        return SupabaseStore(url, key)
    return LocalStore(os.environ.get("LOCAL_DB", os.path.join(ROOT, "data", "local_db.json")))


# ---------------------------------------------------------------------------
# Route logic
# ---------------------------------------------------------------------------

def compute_coverage(leads, visits):
    cluster_of = {l["id"]: l.get("cluster") for l in leads}
    cov = {}
    for v in visits:
        if v["outcome"] == "has_terminal" and v["lead_id"] in cluster_of:
            c = cluster_of[v["lead_id"]]
            cov[c] = cov.get(c, 0) + 1
    return cov


def rescore(leads, visits):
    """Re-score every lead given visits. Returns list of changed rows (id + scoring fields)."""
    cov = compute_coverage(leads, visits)
    status, own = {}, {}
    for v in visits:  # latest visit wins
        status[v["lead_id"]] = STATUS_FOR_OUTCOME[v["outcome"]]
        if v["outcome"] == "has_terminal":
            own[v["lead_id"]] = own.get(v["lead_id"], 0) + 1
    changed = []
    for l in leads:
        # A lead's own "has terminal" visit shouldn't count as a nearby competitor.
        c = l.get("cluster")
        lead_cov = dict(cov, **{c: cov.get(c, 0) - own.get(l["id"], 0)}) if own.get(l["id"]) else cov
        s, parts, reason = score_lead(l, lead_cov)
        st = status.get(l["id"], "open")
        if (s, reason, st) != (l.get("score"), l.get("reason"), l.get("status")):
            l.update(score=s, score_parts=parts, reason=reason, status=st)
            changed.append({"id": l["id"], "score": s, "score_parts": parts,
                            "reason": reason, "status": st})
    return changed


def rank(leads):
    """Open/worked leads first by score; closed ones (has terminal / signed) at the bottom."""
    return sorted(leads, key=lambda l: (l.get("status") in CLOSED_STATUSES, -(l.get("score") or 0),
                                        l.get("business_name") or ""))


def route_leads(store, q):
    leads = rank(store.leads())
    emirate, lane = q.get("emirate"), q.get("lane")
    if emirate and emirate != "All":
        leads = [l for l in leads if l.get("emirate") == emirate]
    if lane and lane != "all":
        leads = [l for l in leads if l.get("lane") == lane]
    for i, l in enumerate(leads, 1):
        l["rank"] = i
    return {"leads": leads, "as_of": AS_OF.isoformat()}


def route_outcome(store, body):
    lead_id, outcome = body.get("lead_id"), body.get("outcome")
    if outcome not in STATUS_FOR_OUTCOME:
        return 400, {"error": f"outcome must be one of {sorted(STATUS_FOR_OUTCOME)}"}
    leads = store.leads()
    target = next((l for l in leads if l["id"] == lead_id), None)
    if not target:
        return 404, {"error": f"unknown lead {lead_id}"}
    before = {l["id"]: l.get("score") for l in leads}
    store.add_visit(lead_id, outcome, body.get("note"))
    changed = rescore(leads, store.visits())
    store.upsert_leads(changed)
    moved = [{"id": c["id"], "name": next(l["business_name"] for l in leads if l["id"] == c["id"]),
              "from": before.get(c["id"]), "to": c["score"]}
             for c in changed if c["id"] != lead_id and c["score"] != before.get(c["id"])]
    return 200, {"ok": True, "lead_id": lead_id, "outcome": outcome,
                 "cluster": target.get("cluster"), "moved": moved}


def route_health(store):
    rows = [merchant_health(m) for m in store.merchants()]
    order = {"At risk": 0, "Watch": 1, "New (ramp-up)": 2, "Healthy": 3}
    rows.sort(key=lambda r: (order[r["status"]], r.get("change_jun_to_sep") or 0))
    summary = {
        "tracked": len(rows),
        "at_risk": sum(r["status"] == "At risk" for r in rows),
        "watch": sum(r["status"] == "Watch" for r in rows),
        "new": sum(r["status"] == "New (ramp-up)" for r in rows),
        "volume_at_risk_sep": sum(r.get("vol_sep") or 0 for r in rows if r["status"] == "At risk"),
    }
    return {"summary": summary, "merchants": rows, "simulated": True}


def route_reset(store):
    store.clear_visits()
    leads = store.leads()
    store.upsert_leads(rescore(leads, []))
    return {"ok": True}


def dispatch(method, path, query, body):
    """Shared by the Vercel handler and dev_server.py. Returns (status, dict)."""
    store = get_store()
    name = path.split("?")[0].rstrip("/").split("/")[-1]
    try:
        if method == "GET" and name == "leads":
            return 200, route_leads(store, query)
        if method == "GET" and name == "health":
            return 200, route_health(store)
        if method == "POST" and name == "outcome":
            return route_outcome(store, body or {})
        if method == "POST" and name == "reset":
            return 200, route_reset(store)
        if method == "GET" and name in ("api", "index", "ping"):
            return 200, {"ok": True, "store": type(store).__name__}
        return 404, {"error": f"no route {method} {path}"}
    except RuntimeError as e:
        return 502, {"error": str(e)}


def _page():
    for p in (os.path.join(ROOT, "index.html"), os.path.join(os.getcwd(), "index.html")):
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
    return None


class handler(BaseHTTPRequestHandler):
    def _run(self, method):
        parsed = urllib.parse.urlparse(self.path)
        # Vercel may route every path (not just /api) to this function: serve the page too.
        if method == "GET" and not parsed.path.startswith("/api"):
            page = _page()
            if page is not None:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
                return
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        # vercel.json rewrites /api/<name> -> /api/index?route=<name>; Vercel then hands us the
        # rewritten path, so recover the real route from the query string.
        route = query.pop("route", None)
        path = "/api/" + route if route else parsed.path
        body = None
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        status, payload = dispatch(method, path, query, body)
        out = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")
