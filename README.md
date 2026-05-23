# ServiceTitan Reporting

A Streamlit dashboard that reports on Jobs and Revenue from the ServiceTitan API, backed by a Postgres cache.

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

2. **Provision a Postgres database.** [Neon](https://neon.tech) is recommended — free tier, serverless, takes ~2 minutes to set up. Sign up, create a project, copy the connection string (looks like `postgresql://user:pass@ep-xxx.us-east-1.aws.neon.tech/neondb?sslmode=require`).

3. Copy `.env.example` to `.env` and fill in your ServiceTitan credentials + `DATABASE_URL`. You need an integration app in the [ServiceTitan developer portal](https://developer.servicetitan.io) with **JPM** + **Accounting** + **Memberships** scopes enabled.

4. Populate the Postgres cache (takes ~5 minutes on first run; ~10 seconds on incremental):
   ```bash
   python scripts/initial_sync.py
   ```

5. Run the app:
   ```bash
   streamlit run app.py
   ```
   It opens at http://localhost:8501.

## Configuration

Credentials and secrets are loaded from `st.secrets` (cloud) or `os.environ` (local). The local `.env` file is loaded automatically via `python-dotenv`.

| Key | Description |
| --- | --- |
| `app_password` | Shared password for the Streamlit gate. If unset, the app is open. |
| `DATABASE_URL` | Postgres connection string. Get one from [Neon](https://neon.tech) (free). |
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

6. **First load:** If the Postgres cache is empty, the first visitor sees "First-time setup detected. Syncing data from ServiceTitan…" with a progress log. After ~5 minutes the cache is built and the dashboard loads. Postgres persists across deploys, so this only happens once (or after data is wiped manually).

### Sharing access

Share the app URL with your team and tell them the password from `app_password`. The Streamlit gate enforces it on every page.

### Ongoing maintenance

- **Refreshing data:** Click **Sync from ServiceTitan** in the sidebar. Incremental sync takes ~10 seconds.
- **Updating code:** Push to your main branch on GitHub. Streamlit auto-redeploys. The Postgres data persists, so no re-sync needed.

## Daily followups email

A GitHub Actions workflow ([.github/workflows/daily_followups_email.yml](.github/workflows/daily_followups_email.yml)) emails a digest of unsold estimates from the rolling last 7 days every morning at 13:00 UTC (8 AM ET / 9 AM EDT). It pulls fresh estimates from ServiceTitan, enriches them with customer phone numbers, and sends an HTML table via Gmail SMTP.

### One-time setup

1. **Generate a Gmail app password.** Go to https://myaccount.google.com/apppasswords (requires 2FA enabled on the account). Pick "Mail" → "Other (Custom name)" → name it `ServiceTitan reporting` → copy the 16-character password.

2. **Add the GitHub repo secrets** at `https://github.com/<your-user>/<your-repo>/settings/secrets/actions`:

   | Secret | Value |
   | --- | --- |
   | `ST_APP_KEY` / `ST_TENANT_ID` / `ST_CLIENT_ID` / `ST_CLIENT_SECRET` / `ST_ENVIRONMENT` | Same as Streamlit Cloud secrets. |
   | `DATABASE_URL` | Same Neon URL as Streamlit Cloud. |
   | `SMTP_USER` | The Gmail address sending the email. |
   | `SMTP_PASSWORD` | The 16-char app password from step 1 (no spaces). |
   | `EMAIL_TO` | Recipient address (your business partner). |
   | `EMAIL_FROM` *(optional)* | Defaults to `SMTP_USER`. Use a display address like `"Pure Air Reports <reports@pureair.com>"` if you want. |

3. **Test it.** From the Actions tab in GitHub, pick "Daily followups email" → "Run workflow". You should get an email within ~1 minute.

### Run manually from your machine

```bash
.venv/bin/python scripts/send_followups_email.py
```

Requires the same env vars in `.env`.

## Weekly AI summary email

A second GitHub Actions workflow ([.github/workflows/weekly_summary_email.yml](.github/workflows/weekly_summary_email.yml)) emails the AI-generated weekly summary (same content as the dashboard's "AI summary" section) every **Friday at 13:00 UTC** (8 AM ET in winter, 9 AM EDT in summer).

Setup: in addition to the followups-email secrets, the workflow needs `ANTHROPIC_API_KEY` in the GitHub Actions secrets. Add it at https://github.com/brettschuenemann/servicetitan-reporting/settings/secrets/actions.

Test manually with: `python scripts/send_weekly_summary_email.py`

## How caching works

- The OAuth access token is cached in-process until ~1 minute before it expires.
- All ServiceTitan data lives in Postgres (`DATABASE_URL`). Reads from the dashboard hit the DB, not the API.
- Sync logic is in [`lib/sync.py`](lib/sync.py); incremental for invoices (via `modifiedOnOrAfter`), full refresh for memberships, fetch-once for billing templates.

## Extending

The client in [`lib/servicetitan.py`](lib/servicetitan.py) exposes `get_jobs`, `get_invoices`, `get_memberships`, `get_business_units`, `get_technicians`. Add new methods using `_paginate()` for any other v2 endpoint. New report pages go in `pages/` — Streamlit picks them up automatically.
