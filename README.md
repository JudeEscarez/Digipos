# Digipos Lead Radar (demo)

Ranks UAE businesses to visit for card-terminal sales, explains each score, and learns from rep feedback.
It also has a merchant health view for existing (simulated) customers.

**Stack:** static `index.html` · one Python serverless function (`api/index.py`, no packages needed) · Supabase · Vercel.

## What's real and what isn't

| Data | Source | Label on screen |
|---|---|---|
| Leads `L001`–`L038` | Real public Google Places listings, collected 17 Oct 2026 | Source link on every lead |
| Leads `X001`–`X010` | **Made up**, showing the format licence data will arrive in | Purple "Example" tag + banner |
| Merchants `M001`–`M030` | **Made up** | "Simulated data" banner |
| WhatsApp drafts | Pre-written templates stored with each lead | "pre-written" |

## Deploy (Vercel)

1. In Vercel, click **Add New → Project** and import `JudeEscarez/Digipos`. Leave the framework preset as **Other**, with no build command.
2. Under **Environment Variables**, add these two:
   - `SUPABASE_URL` = `https://pycaryznonirjmwkybsb.supabase.co`
   - `SUPABASE_SERVICE_ROLE_KEY` = your secret key. Get it from Supabase → Project Settings → API Keys, using either the `service_role` key or a `sb_secret_…` key.
3. Click **Deploy**, then open `/api/ping` on the site. It should return `{"ok": true, "store": "SupabaseStore"}`.

The browser never sees the Supabase key. Every read and write goes through `/api`, and Row Level Security is on with no public policies.

## Run it on a laptop (wifi backup)

```bash
python dev_server.py              # uses Supabase if .env has the keys
OFFLINE=1 python dev_server.py    # no internet needed: runs from data/seed.json
```

Then open http://localhost:8000. In offline mode, your changes are saved to `data/local_db.json`. Delete that file to start fresh.

## Updating the data

When the spreadsheet changes (new rows, oldest-review dates filled in):

```bash
pip install openpyxl
python scripts/import_xlsx.py Digipos_demo_data.xlsx            # refresh data/seed.json (offline copy)
python scripts/import_xlsx.py Digipos_demo_data.xlsx --push     # also update Supabase (needs .env)
```

The import keeps any visits already logged, and recalculates every score. Add `--no-examples` to drop the example new-opening rows, for instance once real Dubai licence data arrives.

## API

| Route | What it does |
|---|---|
| `GET /api/leads?emirate=Dubai&lane=established` | Ranked leads. Leads that already have a terminal or have signed sink to the bottom. |
| `POST /api/outcome` `{"lead_id":"L009","outcome":"has_terminal"}` | Logs a visit and re-scores. Outcomes: `interested`, `not_now`, `has_terminal`, `signed`. |
| `GET /api/health` | Merchant status: Jun→Sep down 25% or more = At risk, down 10–25% = Watch, no June figure = New. |
| `POST /api/reset` | Clears all visits and restores the original ranking, for rehearsals. |

## How scoring works (`api/index.py`, top of file)

The score is out of 100:

- **Newness: 30.** Based on the licence issue date or oldest review date. If neither is filled in, it's 8/30 and the reason says so.
- **Size proxy: 25.** Based on review count and price level. For new openings, it's estimated from category and area instead.
- **Category fit: 20.** Small groups lose 3 points and developer-run businesses lose 5, because a head office may decide.
- **Location: 15.** Based on how busy the area is, **minus 6 for each nearby business a rep marked "already has a terminal"**. This is the "it learns" step.
- **Contactability: 10.** 6 points for a listed phone, 4 for a website.

Two review signals sit outside those weights: "cash only" adds 5, and "cards already accepted" takes away 25. A restricted category scores 0.
Area names are grouped into neighbourhoods by `cluster_for()`. For example, "Al Rigga, Deira" counts as Deira and "Al Quoz 4" counts as Al Quoz.
Every number is an assumption, kept in config dictionaries so it's easy to change.

## 90-second demo path

1. **Leads** tab, then **Dubai**. Show the ranked list.
2. Click **Art of Teeth Dental Clinic** (Al Barsha). Show the score breakdown, the reason and the WhatsApp draft.
3. Tap **Already has a terminal**. The other Al Barsha clinics slide down the list, with "63 → 57" badges.
4. Open the **Merchant health** tab: 5 at risk, AED 195,500 of September volume at risk.
5. Afterwards, click **Reset demo** in the footer.
