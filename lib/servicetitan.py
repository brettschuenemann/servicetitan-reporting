"""Thin client for the ServiceTitan REST API.

OAuth2 client-credentials flow with in-process token caching, automatic
pagination, and a couple of helpers for the endpoints this app uses.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import requests


class ServiceTitanError(RuntimeError):
    pass


class ServiceTitanClient:
    def __init__(
        self,
        app_key: str,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        environment: str = "production",
        page_size: int = 500,
        timeout: int = 60,
    ) -> None:
        if environment == "integration":
            self.auth_url = "https://auth-integration.servicetitan.io/connect/token"
            self.api_base = "https://api-integration.servicetitan.io"
        else:
            self.auth_url = "https://auth.servicetitan.io/connect/token"
            self.api_base = "https://api.servicetitan.io"

        self.app_key = app_key
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.page_size = page_size
        self.timeout = timeout

        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        resp = requests.post(
            self.auth_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ServiceTitanError(
                f"OAuth token request failed ({resp.status_code}): {resp.text}"
            )
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires_at = time.time() + int(body.get("expires_in", 900))
        return self._token

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self._get_token()
        url = f"{self.api_base}{path}"
        for attempt in range(3):
            resp = requests.get(
                url,
                params=params or {},
                headers={
                    "Authorization": f"Bearer {token}",
                    "ST-App-Key": self.app_key,
                },
                timeout=self.timeout,
            )
            if resp.status_code == 429:
                # Respect Retry-After when the server provides it.
                wait = float(resp.headers.get("Retry-After", "2"))
                time.sleep(min(wait, 30))
                continue
            if resp.status_code == 401:
                # Token may have been revoked early; force a refresh and retry once.
                self._token = None
                token = self._get_token()
                continue
            if resp.status_code >= 400:
                raise ServiceTitanError(
                    f"GET {path} failed ({resp.status_code}): {resp.text[:500]}"
                )
            return resp.json()
        raise ServiceTitanError(f"GET {path} failed after retries")

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        merged = dict(params or {})
        merged.setdefault("pageSize", self.page_size)
        page = 1
        while True:
            merged["page"] = page
            body = self._request(path, merged)
            for item in body.get("data", []):
                yield item
            if not body.get("hasMore"):
                break
            page += 1

    # ----- endpoints -----------------------------------------------------

    def get_jobs(
        self,
        created_after: str | None = None,
        created_before: str | None = None,
        modified_after: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if created_after:
            params["createdOnOrAfter"] = created_after
        if created_before:
            params["createdBefore"] = created_before
        if modified_after:
            params["modifiedOnOrAfter"] = modified_after
        path = f"/jpm/v2/tenant/{self.tenant_id}/jobs"
        return list(self._paginate(path, params))

    def get_invoices(
        self,
        created_after: str | None = None,
        created_before: str | None = None,
        modified_after: str | None = None,
        modified_before: str | None = None,
    ) -> list[dict[str, Any]]:
        # The /accounting/v2 invoices endpoint silently ignores invoiceDate-based
        # filter params; only the createdOn and modifiedOn filters actually work.
        params: dict[str, Any] = {}
        if created_after:
            params["createdOnOrAfter"] = created_after
        if created_before:
            params["createdBefore"] = created_before
        if modified_after:
            params["modifiedOnOrAfter"] = modified_after
        if modified_before:
            params["modifiedBefore"] = modified_before
        path = f"/accounting/v2/tenant/{self.tenant_id}/invoices"
        return list(self._paginate(path, params))

    def get_business_units(self) -> list[dict[str, Any]]:
        path = f"/settings/v2/tenant/{self.tenant_id}/business-units"
        return list(self._paginate(path))

    def get_technicians(self) -> list[dict[str, Any]]:
        path = f"/settings/v2/tenant/{self.tenant_id}/technicians"
        return list(self._paginate(path))

    def get_memberships(self) -> list[dict[str, Any]]:
        path = f"/memberships/v2/tenant/{self.tenant_id}/memberships"
        return list(self._paginate(path))

    def get_membership_invoice_template(self, template_id: int) -> dict[str, Any]:
        path = f"/memberships/v2/tenant/{self.tenant_id}/invoice-templates/{template_id}"
        return self._request(path)

    def get_customer_contacts(self, customer_id: int) -> list[dict[str, Any]]:
        path = f"/crm/v2/tenant/{self.tenant_id}/customers/{customer_id}/contacts"
        return list(self._paginate(path))

    def get_campaigns(self) -> list[dict[str, Any]]:
        path = f"/marketing/v2/tenant/{self.tenant_id}/campaigns"
        return list(self._paginate(path))

    def get_all_jobs(self, modified_after: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if modified_after:
            params["modifiedOnOrAfter"] = modified_after
        path = f"/jpm/v2/tenant/{self.tenant_id}/jobs"
        return list(self._paginate(path, params))

    def get_calls(
        self,
        created_after: str | None = None,
        modified_after: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if created_after:
            params["createdAfter"] = created_after
        if modified_after:
            params["modifiedAfter"] = modified_after
        path = f"/telecom/v3/tenant/{self.tenant_id}/calls"
        return list(self._paginate(path, params))

    def get_estimates(
        self,
        created_after: str | None = None,
        created_before: str | None = None,
        modified_after: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if created_after:
            params["createdOnOrAfter"] = created_after
        if created_before:
            params["createdBefore"] = created_before
        if modified_after:
            params["modifiedOnOrAfter"] = modified_after
        path = f"/sales/v2/tenant/{self.tenant_id}/estimates"
        return list(self._paginate(path, params))
