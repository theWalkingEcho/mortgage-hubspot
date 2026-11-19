from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from main import Settings, create_app


class FakeStore:
    def __init__(self):
        self.events = {}

    async def create_or_get(self, lead):
        if lead.event_id in self.events:
            return self.events[lead.event_id], False
        event = {
            **lead.model_dump(mode="json"),
            "status": "PROCESSING",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.events[lead.event_id] = event
        return event, True

    async def update(self, event_id, values):
        self.events[event_id].update(values)
        self.events[event_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

    async def list_recent(self, limit=50):
        return list(self.events.values())[-limit:]

    async def get(self, event_id):
        return self.events.get(event_id)

    async def claim_retry(self, event_id):
        event = self.events[event_id]
        if event["status"] != "FAILED":
            return False
        event["status"] = "PROCESSING"
        event["updated_at"] = datetime.now(timezone.utc).isoformat()
        return True


class FakeCrm:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    async def sync_lead(self, lead):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Simulated HubSpot outage")
        return "contact-123", "deal-456"


def make_client(store=None, crm=None, admin_key="demo-admin-key"):
    settings = Settings(
        supabase_url="https://supabase.example",
        supabase_service_key="server-secret",
        hubspot_access_token="hubspot-secret",
        admin_api_key=admin_key,
        cors_origin=None,
    )
    return TestClient(create_app(settings, store or FakeStore(), crm or FakeCrm()))


def lead_payload(event_id="evt-1"):
    return {
        "event_id": event_id,
        "borrower_name": "Alex Example",
        "email": "alex@example.com",
        "phone": "+61 400 123 456",
        "loan_amount": 780000,
        "property_state": "nsw",
        "referral_partner": "Example Partner",
    }


def auth_headers(key="demo-admin-key"):
    return {"Authorization": f"Bearer {key}"}


def test_health_route_does_not_require_credentials():
    response = TestClient(create_app()).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_ingest_syncs_lead_and_duplicate_is_idempotent():
    store, crm = FakeStore(), FakeCrm()
    client = make_client(store, crm)
    first = client.post(
        "/api/v1/ingest-lead", json=lead_payload(), headers=auth_headers()
    )
    duplicate = client.post(
        "/api/v1/ingest-lead", json=lead_payload(), headers=auth_headers()
    )
    assert first.status_code == 200
    assert first.json()["deal_id"] == "deal-456"
    assert duplicate.json()["status"] == "duplicate_ignored"
    assert store.events["evt-1"]["status"] == "SUCCESS"
    assert crm.calls == 1


def test_ingest_records_failure_for_operator_retry():
    store, crm = FakeStore(), FakeCrm(fail=True)
    client = make_client(store, crm)
    response = client.post(
        "/api/v1/ingest-lead",
        json=lead_payload(),
        headers=auth_headers(),
    )
    assert response.status_code == 502
    assert store.events["evt-1"]["status"] == "FAILED"
    assert "Simulated HubSpot outage" in store.events["evt-1"]["error_message"]


def test_retry_reprocesses_failed_lead_and_updates_same_event():
    store, crm = FakeStore(), FakeCrm(fail=True)
    client = make_client(store, crm)
    first = client.post(
        "/api/v1/ingest-lead", json=lead_payload(), headers=auth_headers()
    )
    assert first.status_code == 502

    crm.fail = False
    retried = client.post(
        "/api/v1/logs/evt-1/retry", headers=auth_headers()
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "synced"
    assert store.events["evt-1"]["status"] == "SUCCESS"
    assert crm.calls == 2


def test_admin_routes_reject_missing_or_invalid_key():
    client = make_client()
    assert client.get("/api/v1/logs").status_code == 401
    assert client.get("/api/v1/logs", headers=auth_headers("wrong")).status_code == 401


def test_admin_login_accepts_admin_and_valid_api_key():
    response = make_client().post(
        "/api/v1/auth/login",
        json={"username": "admin", "api_key": "demo-admin-key"},
    )
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "operator", "api_key": "demo-admin-key"},
        {"username": "admin", "api_key": "wrong"},
    ],
)
def test_admin_login_rejects_invalid_credentials(credentials):
    response = make_client().post("/api/v1/auth/login", json=credentials)
    assert response.status_code == 401


@pytest.mark.parametrize(
    "updates",
    [
        {"loan_amount": 0},
        {"property_state": "XX"},
        {"email": "not-an-email"},
        {"event_id": ""},
    ],
)
def test_payload_rejects_invalid_lead_data(updates):
    payload = lead_payload()
    payload.update(updates)
    response = make_client().post(
        "/api/v1/ingest-lead", json=payload, headers=auth_headers()
    )
    assert response.status_code == 422
