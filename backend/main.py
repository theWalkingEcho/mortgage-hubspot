import asyncio
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class MortgageLeadPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    borrower_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str = Field(default="", max_length=50)
    loan_amount: Decimal = Field(gt=0, le=Decimal("100000000"))
    property_state: str = "NSW"
    referral_partner: str = Field(default="Direct", max_length=200)

    @field_validator("property_state")
    @classmethod
    def validate_property_state(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}:
            raise ValueError("property_state must be an Australian state or territory code")
        return normalized

    @field_validator("borrower_name", "referral_partner")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


@dataclass(frozen=True)
class Settings:
    supabase_url: str | None
    supabase_service_key: str | None
    hubspot_access_token: str | None
    admin_api_key: str | None
    cors_origin: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            supabase_url=os.getenv("SUPABASE_URL"),
            supabase_service_key=(
                os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
            ),
            hubspot_access_token=os.getenv("HUBSPOT_ACCESS_TOKEN"),
            admin_api_key=os.getenv("ADMIN_API_KEY"),
            cors_origin=os.getenv("FRONTEND_ORIGIN"),
        )

    def missing_for_processing(self) -> list[str]:
        missing = []
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.supabase_service_key:
            missing.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.hubspot_access_token:
            missing.append("HUBSPOT_ACCESS_TOKEN")
        if not self.admin_api_key:
            missing.append("ADMIN_API_KEY")
        return missing


class IntegrationError(RuntimeError):
    pass


class IngestError(RuntimeError):
    pass


class EventStore(Protocol):
    async def create_or_get(
        self, lead: MortgageLeadPayload
    ) -> tuple[dict[str, Any], bool]: ...

    async def update(self, event_id: str, values: dict[str, Any]) -> None: ...

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]: ...

    async def get(self, event_id: str) -> dict[str, Any] | None: ...

    async def claim_retry(self, event_id: str) -> bool: ...


class CrmSync(Protocol):
    async def sync_lead(self, lead: MortgageLeadPayload) -> tuple[str, str]: ...


async def request_with_retries(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    for attempt in range(3):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.2 * (2**attempt))
            continue
        if response.status_code not in {408, 425, 429, 500, 502, 503, 504, 529}:
            return response
        if attempt == 2:
            return response
        retry_after = response.headers.get("Retry-After")
        try:
            delay = min(float(retry_after), 5.0) if retry_after else 0.2 * (2**attempt)
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                delay = min(max((retry_time - datetime.now(timezone.utc)).total_seconds(), 0), 5)
            except (TypeError, ValueError, OverflowError):
                delay = 0.2 * (2**attempt)
        await asyncio.sleep(max(delay, 0))
    raise IntegrationError("Request retry loop ended unexpectedly")


def check_response(response: httpx.Response, service: str) -> None:
    if response.is_error:
        raise IntegrationError(f"{service} request failed with HTTP {response.status_code}")


class SupabaseEventStore:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        if not settings.supabase_url or not settings.supabase_service_key:
            raise IntegrationError("Supabase URL and server-side key are not configured")
        self.base_url = settings.supabase_url.rstrip("/") + "/rest/v1/webhook_logs"
        self.client = client or httpx.AsyncClient(timeout=15)
        self.headers = {
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
            "Content-Type": "application/json",
        }

    async def create_or_get(
        self, lead: MortgageLeadPayload
    ) -> tuple[dict[str, Any], bool]:
        record = {
            "event_id": lead.event_id,
            "borrower_name": lead.borrower_name,
            "email": str(lead.email),
            "phone": lead.phone,
            "loan_amount": format(lead.loan_amount, "f"),
            "property_state": lead.property_state,
            "referral_partner": lead.referral_partner,
            "status": "PROCESSING",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        response = await request_with_retries(
            self.client,
            "POST",
            self.base_url,
            params={"on_conflict": "event_id"},
            headers={
                **self.headers,
                "Prefer": "resolution=ignore-duplicates,return=representation",
            },
            json=record,
        )
        check_response(response, "Supabase")
        rows = response.json()
        if rows:
            return rows[0], True
        existing = await self.get(lead.event_id)
        if existing is None:
            raise IntegrationError("Supabase did not return the inserted or existing lead")
        return existing, False

    async def update(self, event_id: str, values: dict[str, Any]) -> None:
        response = await request_with_retries(
            self.client,
            "PATCH",
            self.base_url,
            params={"event_id": f"eq.{event_id}"},
            headers={**self.headers, "Prefer": "return=minimal"},
            json={**values, "updated_at": datetime.now(timezone.utc).isoformat()},
        )
        check_response(response, "Supabase")

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        response = await request_with_retries(
            self.client,
            "GET",
            self.base_url,
            params={
                "select": (
                    "id,event_id,borrower_name,loan_amount,referral_partner,"
                    "hubspot_deal_id,status,error_message,created_at,updated_at"
                ),
                "order": "created_at.desc",
                "limit": str(limit),
            },
            headers=self.headers,
        )
        check_response(response, "Supabase")
        return response.json()

    async def get(self, event_id: str) -> dict[str, Any] | None:
        response = await request_with_retries(
            self.client,
            "GET",
            self.base_url,
            params={"event_id": f"eq.{event_id}", "limit": "1"},
            headers=self.headers,
        )
        check_response(response, "Supabase")
        rows = response.json()
        return rows[0] if rows else None

    async def claim_retry(self, event_id: str) -> bool:
        now = datetime.now(timezone.utc)
        stale_before = (now - timedelta(minutes=5)).isoformat()
        response = await request_with_retries(
            self.client,
            "PATCH",
            self.base_url,
            params={
                "event_id": f"eq.{event_id}",
                "or": (
                    f"(status.eq.FAILED,and(status.eq.PROCESSING,"
                    f"updated_at.lt.{stale_before}))"
                ),
            },
            headers={**self.headers, "Prefer": "return=representation"},
            json={"status": "PROCESSING", "updated_at": now.isoformat()},
        )
        check_response(response, "Supabase")
        return bool(response.json())


class HubSpotClient:
    API_BASE = "https://api.hubapi.com/crm/v3"

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        if not settings.hubspot_access_token:
            raise IntegrationError("HUBSPOT_ACCESS_TOKEN is not configured")
        self.client = client or httpx.AsyncClient(timeout=15)
        self.headers = {
            "Authorization": f"Bearer {settings.hubspot_access_token}",
            "Content-Type": "application/json",
        }

    async def sync_lead(self, lead: MortgageLeadPayload) -> tuple[str, str]:
        contact_id = await self._upsert_contact(lead)
        deal_id = await self._upsert_deal(lead)
        await self._associate_deal_contact(deal_id, contact_id)
        return contact_id, deal_id

    async def _search(
        self, object_type: str, property_name: str, value: str
    ) -> list[dict[str, Any]]:
        requested_properties = ["email"] if object_type == "contacts" else ["external_event_id"]
        response = await request_with_retries(
            self.client,
            "POST",
            f"{self.API_BASE}/objects/{object_type}/search",
            headers=self.headers,
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": property_name,
                                "operator": "EQ",
                                "value": value,
                            }
                        ]
                    }
                ],
                "properties": requested_properties,
                "limit": 1,
            },
        )
        check_response(response, f"HubSpot {object_type} search")
        return response.json().get("results", [])

    async def _upsert_contact(self, lead: MortgageLeadPayload) -> str:
        matches = await self._search("contacts", "email", str(lead.email))
        properties = {
            "email": str(lead.email),
            "firstname": lead.borrower_name.split()[0],
            "lastname": " ".join(lead.borrower_name.split()[1:]),
            "phone": lead.phone,
        }
        if matches:
            contact_id = str(matches[0]["id"])
            response = await request_with_retries(
                self.client,
                "PATCH",
                f"{self.API_BASE}/objects/contacts/{quote(contact_id, safe='')}",
                headers=self.headers,
                json={"properties": properties},
            )
        else:
            response = await request_with_retries(
                self.client,
                "POST",
                f"{self.API_BASE}/objects/contacts",
                headers=self.headers,
                json={"properties": properties},
            )
            if response.status_code == 409:
                matches = await self._search("contacts", "email", str(lead.email))
                if matches:
                    contact_id = str(matches[0]["id"])
                    response = await request_with_retries(
                        self.client,
                        "PATCH",
                        f"{self.API_BASE}/objects/contacts/{quote(contact_id, safe='')}",
                        headers=self.headers,
                        json={"properties": properties},
                    )
                else:
                    check_response(response, "HubSpot contact create")
                    raise IntegrationError("HubSpot contact conflict could not be resolved")
            else:
                check_response(response, "HubSpot contact create")
                contact_id = str(response.json()["id"])
        check_response(response, "HubSpot contact update")
        return contact_id

    async def _upsert_deal(self, lead: MortgageLeadPayload) -> str:
        matches = await self._search("deals", "external_event_id", lead.event_id)
        properties = {
            "dealname": f"Home Loan - {lead.borrower_name}",
            "amount": str(lead.loan_amount),
            "loan_amount": str(lead.loan_amount),
            "property_state": lead.property_state,
            "referral_partner": lead.referral_partner,
            "external_event_id": lead.event_id,
        }
        if matches:
            deal_id = str(matches[0]["id"])
            response = await request_with_retries(
                self.client,
                "PATCH",
                f"{self.API_BASE}/objects/deals/{quote(deal_id, safe='')}",
                headers=self.headers,
                json={"properties": properties},
            )
            check_response(response, "HubSpot deal update")
            return deal_id
        response = await request_with_retries(
            self.client,
            "POST",
            f"{self.API_BASE}/objects/deals",
            headers=self.headers,
            json={"properties": properties},
        )
        check_response(response, "HubSpot deal create")
        return str(response.json()["id"])

    async def _associate_deal_contact(self, deal_id: str, contact_id: str) -> None:
        response = await request_with_retries(
            self.client,
            "PUT",
            (
                f"https://api.hubapi.com/crm/v4/objects/deals/{quote(deal_id, safe='')}"
                f"/associations/default/contacts/{quote(contact_id, safe='')}"
            ),
            headers=self.headers,
        )
        check_response(response, "HubSpot deal/contact association")


class LeadService:
    def __init__(self, store: EventStore, crm: CrmSync) -> None:
        self.store = store
        self.crm = crm

    async def ingest(self, lead: MortgageLeadPayload) -> dict[str, str]:
        event, created = await self.store.create_or_get(lead)
        if not created:
            return {"status": "duplicate_ignored", "event_id": lead.event_id}
        return await self._sync(lead)

    async def retry(self, event_id: str) -> dict[str, str]:
        event = await self.store.get(event_id)
        if event is None:
            raise LookupError("Lead event was not found")
        if event.get("status") not in {"FAILED", "PROCESSING"}:
            raise ValueError(f"Cannot retry a lead with status {event.get('status')}")
        if event.get("status") == "PROCESSING" and not self._is_stale(event):
            raise ValueError("Lead is already being processed")
        if not await self.store.claim_retry(event_id):
            raise ValueError("Lead is already being processed or cannot be retried")
        lead = MortgageLeadPayload(
            event_id=event["event_id"],
            borrower_name=event["borrower_name"],
            email=event["email"],
            phone=event.get("phone") or "",
            loan_amount=event["loan_amount"],
            property_state=event.get("property_state") or "NSW",
            referral_partner=event.get("referral_partner") or "Direct",
        )
        return await self._sync(lead)

    async def _sync(self, lead: MortgageLeadPayload) -> dict[str, str]:
        try:
            contact_id, deal_id = await self.crm.sync_lead(lead)
        except Exception as exc:
            await self.store.update(
                lead.event_id,
                {"status": "FAILED", "error_message": str(exc)[:500]},
            )
            raise IngestError("CRM sync failed; the event was recorded for review") from exc
        await self.store.update(
            lead.event_id,
            {
                "status": "SUCCESS",
                "hubspot_deal_id": deal_id,
                "error_message": None,
            },
        )
        return {
            "status": "synced",
            "event_id": lead.event_id,
            "deal_id": deal_id,
            "contact_id": contact_id,
        }

    @staticmethod
    def _is_stale(event: dict[str, Any]) -> bool:
        updated_at = event.get("updated_at")
        if not isinstance(updated_at, str):
            return False
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated > timedelta(minutes=5)


def create_app(
    settings: Settings | None = None,
    store: EventStore | None = None,
    crm: CrmSync | None = None,
) -> FastAPI:
    configured = settings or Settings.from_env()
    application = FastAPI(title="Mortgage CRM Automation API", version="1.0.0")
    application.state.settings = configured
    application.state.store = store
    application.state.crm = crm
    if configured.cors_origin:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=[configured.cors_origin.rstrip("/")],
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    def get_service() -> LeadService:
        missing = configured.missing_for_processing()
        if missing:
            raise HTTPException(
                status_code=503,
                detail=f"Server configuration missing: {', '.join(missing)}",
            )
        if application.state.store is None:
            application.state.store = SupabaseEventStore(configured)
        if application.state.crm is None:
            application.state.crm = HubSpotClient(configured)
        return LeadService(application.state.store, application.state.crm)

    async def require_admin(authorization: str | None = Header(default=None)) -> None:
        if not configured.admin_api_key:
            raise HTTPException(status_code=503, detail="ADMIN_API_KEY is not configured")
        supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
        if not secrets.compare_digest(supplied, configured.admin_api_key):
            raise HTTPException(status_code=401, detail="Invalid API key")

    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "mortgage-automation"}

    @application.post("/api/v1/ingest-lead")
    async def ingest_lead(
        payload: MortgageLeadPayload,
        _: None = Depends(require_admin),
        service: LeadService = Depends(get_service),
    ) -> dict[str, str]:
        try:
            return await service.ingest(payload)
        except IngestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except IntegrationError as exc:
            raise HTTPException(status_code=502, detail="Lead could not be staged in Supabase") from exc

    @application.get("/api/v1/logs")
    async def recent_logs(
        _: None = Depends(require_admin),
        service: LeadService = Depends(get_service),
    ) -> list[dict[str, Any]]:
        try:
            return await service.store.list_recent()
        except IntegrationError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @application.post("/api/v1/logs/{event_id}/retry")
    async def retry_lead(
        event_id: str,
        _: None = Depends(require_admin),
        service: LeadService = Depends(get_service),
    ) -> dict[str, str]:
        try:
            return await service.retry(event_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IngestError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except IntegrationError as exc:
            raise HTTPException(status_code=502, detail="Lead could not be loaded from Supabase") from exc

    return application


app = create_app()
