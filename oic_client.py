"""
oic_client.py — Reusable OIC REST API client.

Provides BearerAuthSession and OICClient for use by oic_sync.py, clear_oic.py,
and any other scripts that need to talk to Oracle Integration Cloud.
"""

import time

import requests


# ---------------------------------------------------------------------------
# BearerAuthSession
# ---------------------------------------------------------------------------

class BearerAuthSession(requests.Session):
    """
    Session that injects a Bearer token on every request and follows redirects
    manually so the Authorization header is forwarded to the redirect target.
    Replicates curl's --location-trusted behaviour.
    """

    def __init__(self, token: str | None = None, verify_ssl: bool = True):
        super().__init__()
        self.token = token
        self.verify_ssl = verify_ssl
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def request(self, method, url, *args, **kwargs):
        kwargs["verify"] = self.verify_ssl
        kwargs["allow_redirects"] = False
        if "headers" not in kwargs:
            kwargs["headers"] = {}
        kwargs["headers"]["Authorization"] = f"Bearer {self.token}"
        response = super().request(method, url, *args, **kwargs)
        while response.is_redirect or response.is_permanent_redirect:
            redirect_url = response.headers["Location"]
            response = super().request(method, redirect_url, *args, **kwargs)
        return response


# ---------------------------------------------------------------------------
# OICClient
# ---------------------------------------------------------------------------

class OICClient:
    """Wraps OIC REST API calls for a single environment."""

    BASE_PATH = "/ic/api/integration/v1"

    def __init__(self, idcs_host: str, client_id: str, client_secret: str, scope: str, oic_host: str, label: str = "", verify_ssl: bool = True):
        self.idcs_host = idcs_host
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.oic_host = oic_host
        self.label = label or oic_host
        self.verify_ssl = verify_ssl
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._session = BearerAuthSession(verify_ssl=verify_ssl)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _refresh_token(self) -> None:
        url = f"https://{self.idcs_host}/oauth2/v1/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": self.scope,
        }
        resp = requests.post(url, data=data, verify=self.verify_ssl, timeout=30)
        self._check_response(resp, "token")
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 3600)) - 30  # 30-s buffer
        self._session.token = self._token

    def _ensure_token(self) -> None:
        if not self._token or time.time() >= self._token_expiry:
            self._refresh_token()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _encode_id(self, integration_id: str) -> str:
        """URL-encode the pipe character in an integration ID."""
        return integration_id.replace("|", "%7C")

    def _resource_url(self, resource: str) -> str:
        return f"https://{self.oic_host}{self.BASE_PATH}/{resource}"

    def _base_url(self) -> str:
        """Backward-compatible alias for the integrations resource URL."""
        return self._resource_url("integrations")

    def _check_response(self, resp: requests.Response, operation: str = "") -> None:
        """Raise a descriptive HTTPError for 4xx/5xx responses."""
        if resp.ok:
            return
        prefix = f"[{operation}] " if operation else ""
        try:
            body = resp.json()
            detail = (
                body.get("detail")
                or body.get("title")
                or body.get("message")
                or str(body)
            )
        except ValueError:
            detail = resp.text or resp.reason
        raise requests.HTTPError(f"{prefix}HTTP {resp.status_code}: {detail}", response=resp)

    def _paginate(self, base_url: str, has_more_key: str = "hasMore", operation: str = "") -> list[dict]:
        """Fetch all pages from a paginated OIC list endpoint."""
        items = []
        offset = 0
        limit = 100
        while True:
            url = f"{base_url}?offset={offset}"
            resp = self._session.get(url, timeout=30)
            self._check_response(resp, operation)
            body = resp.json()
            items.extend(body.get("items", []))
            if not body.get(has_more_key, False):
                break
            limit = body.get("limit", limit)
            offset += limit
        return items

    # ------------------------------------------------------------------
    # Integrations
    # ------------------------------------------------------------------

    def list_integrations(self) -> list[dict]:
        """Return all integrations (handles pagination)."""
        self._ensure_token()
        return self._paginate(self._base_url(), has_more_key="hasMore", operation="list integrations")

    def get_integration(self, integration_id: str) -> dict | None:
        """Return integration details, or None if not found."""
        self._ensure_token()
        url = f"{self._base_url()}/{self._encode_id(integration_id)}"
        resp = self._session.get(url, timeout=30)
        if resp.status_code == 404:
            return None
        self._check_response(resp, f"get {integration_id}")
        return resp.json()

    def download_archive(self, integration_id: str) -> bytes:
        """Download an integration archive (.iar) and return its raw bytes."""
        self._ensure_token()
        url = f"{self._base_url()}/{self._encode_id(integration_id)}/archive"
        resp = self._session.get(url, timeout=120)
        self._check_response(resp, f"download {integration_id}")
        return resp.content

    def import_integration(self, iar_bytes: bytes, exists: bool) -> None:
        """Import (POST) or replace (PUT) an integration archive."""
        self._ensure_token()
        url = f"{self._base_url()}/archive"
        files = {"file": ("integration.iar", iar_bytes, "application/octet-stream")}
        method = self._session.put if exists else self._session.post
        resp = method(url, files=files, timeout=120)
        self._check_response(resp, "import")

    def _set_status(self, integration_id: str, status: str) -> None:
        self._ensure_token()
        url = f"{self._base_url()}/{self._encode_id(integration_id)}"
        headers = {"X-HTTP-Method-Override": "PATCH", "Content-Type": "application/json"}
        resp = self._session.post(url, headers=headers, json={"status": status}, timeout=60)
        self._check_response(resp, f"set status {integration_id} → {status}")

    def activate_integration(self, integration_id: str) -> None:
        self._set_status(integration_id, "ACTIVATED")

    def deactivate_integration(self, integration_id: str) -> None:
        self._set_status(integration_id, "CONFIGURED")

    def delete_integration(self, integration_id: str) -> None:
        """Delete an integration. Must be deactivated first (else 423)."""
        self._ensure_token()
        url = f"{self._base_url()}/{self._encode_id(integration_id)}"
        resp = self._session.delete(url, timeout=30)
        self._check_response(resp, f"delete integration {integration_id}")

    # ------------------------------------------------------------------
    # Connections
    # ------------------------------------------------------------------

    def list_connections(self) -> list[dict]:
        """Return all connections (handles pagination)."""
        self._ensure_token()
        return self._paginate(self._resource_url("connections"), has_more_key="hasMore", operation="list connections")

    def delete_connection(self, conn_id: str) -> None:
        """Delete a connection. Locked connections return 423."""
        self._ensure_token()
        url = f"{self._resource_url('connections')}/{conn_id}"
        resp = self._session.delete(url, timeout=30)
        self._check_response(resp, f"delete connection {conn_id}")

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def list_lookups(self) -> list[dict]:
        """Return all lookups (handles pagination)."""
        self._ensure_token()
        # Lookups API uses kebab-case "has-more" instead of camelCase "hasMore"
        return self._paginate(self._resource_url("lookups"), has_more_key="has-more", operation="list lookups")

    def delete_lookup(self, name: str) -> None:
        """Delete a lookup. Locked lookups return 423."""
        self._ensure_token()
        url = f"{self._resource_url('lookups')}/{name}"
        resp = self._session.delete(url, timeout=30)
        self._check_response(resp, f"delete lookup {name}")

    # ------------------------------------------------------------------
    # Packages
    # ------------------------------------------------------------------

    def list_packages(self) -> list[dict]:
        """Return all packages (handles pagination)."""
        self._ensure_token()
        # Packages API uses kebab-case "has-more" instead of camelCase "hasMore"
        return self._paginate(self._resource_url("packages"), has_more_key="has-more", operation="list packages")

    def delete_package(self, name: str) -> None:
        """Delete a package. Returns 412 if non-empty or non-deletable type."""
        self._ensure_token()
        url = f"{self._resource_url('packages')}/{name}"
        resp = self._session.delete(url, timeout=30)
        self._check_response(resp, f"delete package {name}")
