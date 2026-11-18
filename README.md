# Mortgage Lead Automation HubSpot Demo

This is a demo application for submitting Australian mortgage referrals, upserting a HubSpot contact, creating/updating and associating a HubSpot deal, and storing a private audit log in Supabase. It contains a FastAPI backend and a Next.js operations dashboard.

## Security decisions

- The dashboard calls the backend; it does not connect directly to Supabase.
- `SUPABASE_SERVICE_ROLE_KEY`, the HubSpot token, and `ADMIN_API_KEY` stay in backend environment variables. Never use these as `NEXT_PUBLIC_*`.
- Supabase RLS is enabled and there is no public read policy. The backend service key is the only database access path in this demo.
- Backend routes other than `/api/health` require `Authorization: Bearer <ADMIN_API_KEY>`.
- Use fictional test data only. Mortgage and contact details are sensitive personal information; do not use this sample as a production lending system without a privacy, security, access-control, retention, and compliance review.

## 1. Create Supabase database

1. Create a Supabase project and open its SQL Editor.
2. Run [`supabase/schema.sql`](supabase/schema.sql).
3. Copy the project URL and server-side secret key from the Supabase project **Connect** dialog/API settings. The secret/service-role key bypasses RLS; keep it on the backend only. Do not create a public-read policy.

## 2. Configure HubSpot

1. Use a HubSpot test account where possible. Create a HubSpot Developer Platform app with static auth for a single account and grant the contact/deal read/write and deal-schema read/write scopes required for this demo.
2. In HubSpot **Automations → Workflows**, add a **Send a webhook** POST action for the broker/form lead source. Set the target to your deployed backend URL plus `/api/v1/ingest-lead`, configure an `Authorization: Bearer <ADMIN_API_KEY>` header, and map the request body to the JSON shape below. Set `event_id` to a stable, unique source referral ID so redeliveries use the same value. Do not point a Developer Platform CRM-event webhook subscription at this endpoint: those subscriptions send a different event-batch format. If the workflow action cannot send the required authorization header or custom body, use a small trusted middleware to map and authenticate its request.
3. Set `HUBSPOT_ACCESS_TOKEN` in the backend environment. For local setup, set it only in the terminal running the script:

   ```powershell
   $env:HUBSPOT_ACCESS_TOKEN = "your-app-access-token"
   python scripts\setup_hubspot.py
   ```

4. The setup script creates deal properties `loan_amount`, `property_state`, `referral_partner`, and `external_event_id`. Rerunning it skips properties already created.

Example request:

```json
{
  "event_id": "broker-referral-unique-id",
  "borrower_name": "Alex Example",
  "email": "alex@example.com",
  "phone": "+61 400 123 456",
  "loan_amount": 780000,
  "property_state": "NSW",
  "referral_partner": "Example Accounting"
}
```

`event_id` is the idempotency key and must stay the same when retrying the same referral. The backend uses it to avoid duplicate deals. A different event ID represents a new referral.

## 3. Run locally

### Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
$env:SUPABASE_URL = "https://YOUR_PROJECT.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "your-server-side-secret"
$env:HUBSPOT_ACCESS_TOKEN = "your-app-access-token"
$env:ADMIN_API_KEY = "a-long-random-secret"
$env:FRONTEND_ORIGIN = "http://localhost:3000"
uvicorn main:app --reload
```

Health check: `http://127.0.0.1:8000/api/health`.

### Frontend

In another terminal:

```powershell
cd frontend
npm install
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8000"
npm run dev
```

Open `http://localhost:3000` and enter the backend `ADMIN_API_KEY` in the dashboard. The dashboard API key stays in page state and is not written to local storage.
The frontend requires Node.js 20.9 or later; Node 22 LTS is recommended.

## 4. Deploy as two Vercel projects

Use the same GitHub repository for both projects, with separate root directories:

### Backend Vercel project

- Root Directory: `backend`
- Framework/runtime: Python / FastAPI (entry point `main.py`)
- Environment variables: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `HUBSPOT_ACCESS_TOKEN`, `ADMIN_API_KEY`, and `FRONTEND_ORIGIN` (the frontend's HTTPS origin).
- Deploy and copy the backend URL.

### Frontend Vercel project

- Root Directory: `frontend`
- Framework preset: Next.js
- Environment variable: `NEXT_PUBLIC_API_URL` set to the backend URL.
- Deploy and open the dashboard.

After both are deployed, set the exact frontend origin in the backend's `FRONTEND_ORIGIN` and redeploy the backend. The backend also authenticates every non-health API request, so CORS is not the access-control mechanism.

## API routes

- `GET /api/health` — public liveness check.
- `POST /api/v1/ingest-lead` — validate and sync one lead; requires the admin bearer key.
- `GET /api/v1/logs` — read the latest 50 audit records; requires the admin bearer key.
- `POST /api/v1/logs/{event_id}/retry` — retry a failed or stale processing event; requires the admin bearer key.

## Tests

```powershell
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

Tests use fake CRM/database integrations and do not require live credentials.
