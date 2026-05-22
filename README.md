# ServiceTitan Reporting

A Streamlit dashboard that reports on Jobs and Revenue from the ServiceTitan API, backed by a local SQLite cache.

## What's inside

- **Home dashboard** — KPI cards (total jobs, completed jobs, total revenue, avg invoice), YTD comparison vs prior years, cumulative annual revenue line chart, monthly revenue chart, jobs by status, year-over-year bar chart, separate informational section for maintenance contracts, recent jobs.
- **Jobs report** — filterable table of jobs by status, daily trend, CSV export.
- **Revenue report** — revenue trend, top business units, top customers, CSV export.

Revenue is computed as the sum of invoice totals by `invoiceDate`. This matches the user's accountant's "Total for Income" to within 0.5% over Jan 2024 – Sep 2025. Maintenance contracts are shown separately but not added to revenue (doing so double-counts ~$120k/year due to a migration artifact in this tenant).

## Run locally

1. Install dependencies (use a virtualenv if you like):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your ServiceTitan credentials. You need an integration app in the [ServiceTitan developer portal](https://developer.servicetitan.io) with **JPM** + **Accounting** + **Memberships** scopes enabled.

3. Populate the local SQLite cache (takes ~5 minutes on first run; ~10 seconds on incremental):
   ```bash
   python scripts/initial_sync.py
   ```

4. Run the app:
   ```bash
   streamlit run app.py
   ```
   It opens at http://localhost:8501.

## Configuration

Credentials and secrets are loaded from `st.secrets` (cloud) or `os.environ` (local). The local `.env` file is loaded automatically via `python-dotenv`.

| Key | Description |
| --- | --- |
| `app_password` | Shared password for the Streamlit gate. If unset, the app is open. |
| `ST_APP_KEY` | App key from the integration app (sent as `ST-App-Key` header). |
| `ST_TENANT_ID` | Your tenant ID (numeric). |
| `ST_CLIENT_ID` | OAuth client ID. |
| `ST_CLIENT_SECRET` | OAuth client secret. |
| `ST_ENVIRONMENT` | `production` (default) or `integration`. |

## Deploy to Streamlit Community Cloud

Streamlit Community Cloud (free) is the simplest path for a 2-5 person internal team.

### One-time setup

1. **Make sure secrets won't leak.** `.env`, `data/`, and `.streamlit/secrets.toml` are already in `.gitignore`. Double-check with `git status` before pushing.

2. **Initialize git and push to GitHub:**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   gh repo create servicetitan-reporting --private --source=. --push
   # Or via web UI: create a repo at https://github.com/new, then:
   #   git remote add origin git@github.com:YOUR_USER/servicetitan-reporting.git
   #   git push -u origin main
   ```

3. **Sign in to Streamlit Cloud** at https://share.streamlit.io with your GitHub account.

4. **Deploy:** Click **New app** → pick your repo, branch `main`, main file `app.py`. Wait for the first build (~2 min for dependencies).

5. **Set secrets:** In the deployed app's settings → **Secrets**, paste the contents of `.streamlit/secrets.toml.example` (with real values). Save. The app will restart.

6. **First load:** The very first visitor will see "First-time setup detected. Syncing data from ServiceTitan…" with a progress log. After ~5 minutes the SQLite cache is built and the dashboard loads. Subsequent visits are instant until the container restarts.

### Sharing access

Share the app URL with your team and tell them the password from `app_password`. The Streamlit gate enforces it on every page.

### Ongoing maintenance

- **Refreshing data:** Click **Sync from ServiceTitan** in the sidebar. Incremental sync takes ~10 seconds.
- **Updating code:** Push to your main branch on GitHub. Streamlit auto-redeploys. The DB gets wiped on redeploy and re-syncs automatically on first load (~5 min).
- **If you want persistent storage across deploys** (skip the 5-min re-sync after every push): switch hosts to Render or Fly.io with a 1 GB persistent volume mounted at `data/`, or modify the sync to push/pull the `.db` file to S3/R2 between deploys.

## How caching works

- The OAuth access token is cached in-process until ~1 minute before it expires.
- All ServiceTitan data lives in `data/servicetitan.db` (SQLite). Reads from the dashboard hit the DB, not the API.
- Sync logic is in [`lib/sync.py`](lib/sync.py); incremental for invoices (via `modifiedOnOrAfter`), full refresh for memberships, fetch-once for billing templates.

## Extending

The client in [`lib/servicetitan.py`](lib/servicetitan.py) exposes `get_jobs`, `get_invoices`, `get_memberships`, `get_business_units`, `get_technicians`. Add new methods using `_paginate()` for any other v2 endpoint. New report pages go in `pages/` — Streamlit picks them up automatically.
