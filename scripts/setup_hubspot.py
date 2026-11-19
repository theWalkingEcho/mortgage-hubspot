import asyncio
import os

import httpx


PROPERTY_DEFINITIONS = [
    {
        "name": "loan_amount",
        "label": "Loan amount",
        "type": "number",
        "fieldType": "number",
        "description": "Requested mortgage loan amount in AUD.",
    },
    {
        "name": "property_state",
        "label": "Property state",
        "type": "enumeration",
        "fieldType": "select",
        "options": [
            {"label": state, "value": state, "displayOrder": order}
            for order, state in enumerate(
                ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]
            )
        ],
    },
    {
        "name": "referral_partner",
        "label": "Referral partner",
        "type": "string",
        "fieldType": "text",
    },
    {
        "name": "external_event_id",
        "label": "External lead event ID",
        "type": "string",
        "fieldType": "text",
        "hasUniqueValue": True,
    },
]


async def main() -> None:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Set HUBSPOT_ACCESS_TOKEN to a HubSpot app access token.")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        timeout=20, headers=headers, base_url="https://api.hubapi.com"
    ) as client:
        for definition in PROPERTY_DEFINITIONS:
            check = await client.get(
                f"/crm/v3/properties/deals/{definition['name']}"
            )
            if check.status_code == 200:
                print(f"Exists: deal.{definition['name']}")
                continue
            if check.status_code != 404:
                check.raise_for_status()
            response = await client.post(
                "/crm/v3/properties/deals",
                json={**definition, "groupName": "dealinformation"},
            )
            response.raise_for_status()
            print(f"Created: deal.{definition['name']}")

    print("HubSpot deal properties are ready.")


if __name__ == "__main__":
    asyncio.run(main())
