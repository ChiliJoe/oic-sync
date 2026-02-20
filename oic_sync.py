#!/usr/bin/env python3
"""
oic_sync.py — Sync Oracle Integration Cloud integrations from source to target.

Designed for cron scheduling. Exits with 0 on success, 1 if any integration failed.

Usage:
    python oic_sync.py [--dry-run] [--yes]

    --dry-run   Show what would be deployed without making any changes.
    --yes, -y   Skip the confirmation prompt and deploy immediately.

Requirements:
    pip install requests python-dotenv

Environment variables (loaded from .env):
    SOURCE_IDCS_HOST, SOURCE_CLIENT_ID, SOURCE_CLIENT_SECRET, SOURCE_SCOPE, SOURCE_OIC_HOST
    TARGET_IDCS_HOST, TARGET_CLIENT_ID, TARGET_CLIENT_SECRET, TARGET_SCOPE, TARGET_OIC_HOST
    ACTIVATE_ON_DEPLOY  (true/false, default: true)
    INTEGRATIONS_FILE   (optional path to file with integration IDs to sync)
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

LOG_FILE = f"oic-sync-{datetime.now().strftime('%Y%m%d%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OICClient
# ---------------------------------------------------------------------------

class OICClient:
    """Wraps OIC REST API calls for a single environment."""

    BASE_PATH = "/ic/api/integration/v1/integrations"

    def __init__(self, idcs_host: str, client_id: str, client_secret: str, scope: str, oic_host: str, label: str = ""):
        self.idcs_host = idcs_host
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.oic_host = oic_host
        self.label = label or oic_host
        self._token: str | None = None
        self._token_expiry: float = 0.0

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
        resp = requests.post(url, data=data, verify=False, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 3600)) - 30  # 30-s buffer

    def _headers(self) -> dict:
        if not self._token or time.time() >= self._token_expiry:
            self._refresh_token()
        return {"Authorization": f"Bearer {self._token}"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _encode_id(self, integration_id: str) -> str:
        """URL-encode the pipe character in an integration ID."""
        return integration_id.replace("|", "%7C")

    def _base_url(self) -> str:
        return f"https://{self.oic_host}{self.BASE_PATH}"

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    def list_integrations(self) -> list[dict]:
        """Return all integrations (handles pagination)."""
        integrations = []
        offset = 0
        limit = 100
        while True:
            url = f"{self._base_url()}?offset={offset}"
            resp = requests.get(url, headers=self._headers(), verify=False, timeout=30)
            resp.raise_for_status()
            body = resp.json()
            items = body.get("items", [])
            integrations.extend(items)
            if not body.get("hasMore", False):
                break
            limit = body.get("limit", limit)
            offset += limit
        return integrations

    def get_integration(self, integration_id: str) -> dict | None:
        """Return integration details, or None if not found."""
        url = f"{self._base_url()}/{self._encode_id(integration_id)}"
        resp = requests.get(url, headers=self._headers(), verify=False, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def download_archive(self, integration_id: str) -> bytes:
        """Download an integration archive (.iar) and return its raw bytes."""
        url = f"{self._base_url()}/{self._encode_id(integration_id)}/archive"
        resp = requests.get(url, headers=self._headers(), verify=False, timeout=120)
        resp.raise_for_status()
        return resp.content

    def import_integration(self, iar_bytes: bytes, exists: bool) -> None:
        """Import (POST) or replace (PUT) an integration archive."""
        url = f"{self._base_url()}/archive"
        method = requests.put if exists else requests.post
        files = {"file": ("integration.iar", iar_bytes, "application/octet-stream")}
        resp = method(url, headers=self._headers(), files=files, verify=False, timeout=120)
        if resp.status_code not in (200, 204):
            raise requests.HTTPError(
                f"Import failed [{resp.status_code}]: {resp.text}", response=resp
            )

    def _set_status(self, integration_id: str, status: str) -> None:
        url = f"{self._base_url()}/{self._encode_id(integration_id)}"
        headers = {**self._headers(), "X-HTTP-Method-Override": "PATCH", "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json={"status": status}, verify=False, timeout=60)
        resp.raise_for_status()

    def activate_integration(self, integration_id: str) -> None:
        self._set_status(integration_id, "ACTIVATED")

    def deactivate_integration(self, integration_id: str) -> None:
        self._set_status(integration_id, "CONFIGURED")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_integrations_file(path: str | None) -> set[str] | None:
    """Load allowed integration IDs from a file. Returns None if no file configured."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        logger.error("INTEGRATIONS_FILE not found: %s", path)
        sys.exit(1)
    ids = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.add(line)
    logger.info("Loaded %d integration ID(s) from %s", len(ids), path)
    return ids


# ---------------------------------------------------------------------------
# Core sync logic — phase 1: planning
# ---------------------------------------------------------------------------

def collect_pending(
    source: OICClient,
    target: OICClient,
    allowed_ids: set[str] | None,
) -> tuple[list[dict], int]:
    """
    Determine which integrations need to be deployed.

    Returns (pending, skipped) where pending is a list of dicts with keys:
        id, source_status, source_ts, exists_in_target, target_status, target_ts, action
    and skipped is the count of integrations excluded by filter or already up to date.
    """
    logger.info("Fetching integration list from source (%s)...", source.label)
    source_integrations = source.list_integrations()
    logger.info("Found %d integration(s) in source.", len(source_integrations))

    pending = []
    skipped = 0

    for integration in source_integrations:
        int_id = integration["id"]
        source_status = integration.get("status", "CONFIGURED")
        source_ts = integration.get("lastUpdated", "")

        if allowed_ids is not None and int_id not in allowed_ids:
            logger.debug("SKIP [%s] — not in integrations file", int_id)
            skipped += 1
            continue

        target_int = target.get_integration(int_id)
        exists = target_int is not None
        target_status = target_int.get("status") if exists else None
        target_ts = target_int.get("lastUpdated", "") if exists else ""

        if exists and target_ts and source_ts and target_ts >= source_ts:
            logger.info(
                "SKIP [%s] — target is up to date (target=%s, source=%s)",
                int_id, target_ts, source_ts,
            )
            skipped += 1
            continue

        action_parts = []
        if exists:
            action_parts.append("UPDATE")
            if target_status == "ACTIVATED":
                action_parts.append("deactivate first")
        else:
            action_parts.append("IMPORT")
        if source_status == "ACTIVATED":
            action_parts.append("then ACTIVATE")

        pending.append({
            "id": int_id,
            "source_status": source_status,
            "source_ts": source_ts,
            "exists_in_target": exists,
            "target_status": target_status,
            "target_ts": target_ts,
            "action": ", ".join(action_parts),
        })

    return pending, skipped


def print_plan(pending: list[dict], activate_on_deploy: bool) -> None:
    """Print a human-readable table of integrations to be deployed."""
    if not pending:
        print("\nNothing to deploy.")
        return

    col_id = max(len(p["id"]) for p in pending)
    col_id = max(col_id, len("INTEGRATION ID"))

    header = f"  {'INTEGRATION ID':<{col_id}}  {'SOURCE':<10}  {'TARGET':<10}  ACTION"
    print(f"\nIntegrations to deploy ({len(pending)}):")
    print(f"  {'-' * (len(header) - 2)}")
    print(header)
    print(f"  {'-' * (len(header) - 2)}")
    for p in pending:
        target_col = p["target_status"] or "not found"
        action = p["action"] if activate_on_deploy else p["action"].replace(", then ACTIVATE", "")
        print(f"  {p['id']:<{col_id}}  {p['source_status']:<10}  {target_col:<10}  {action}")
    print()


# ---------------------------------------------------------------------------
# Core sync logic — phase 2: deployment
# ---------------------------------------------------------------------------

def deploy_pending(
    source: OICClient,
    target: OICClient,
    pending: list[dict],
    activate_on_deploy: bool,
) -> tuple[int, int]:
    """
    Deploy all pending integrations.

    Returns (synced, failed) counts.
    """
    synced = failed = 0

    for item in pending:
        int_id = item["id"]
        exists = item["exists_in_target"]
        source_status = item["source_status"]
        target_status = item["target_status"]

        try:
            logger.info("Downloading archive for [%s] from source...", int_id)
            iar_bytes = source.download_archive(int_id)

            if exists and target_status == "ACTIVATED":
                logger.info("Deactivating [%s] in target...", int_id)
                target.deactivate_integration(int_id)

            action = "Updating" if exists else "Importing"
            logger.info("%s [%s] in target...", action, int_id)
            target.import_integration(iar_bytes, exists=exists)

            if activate_on_deploy and source_status == "ACTIVATED":
                logger.info("Activating [%s] in target...", int_id)
                target.activate_integration(int_id)

            logger.info("SYNCED [%s]", int_id)
            synced += 1

        except Exception as exc:
            logger.error("FAILED [%s] — %s", int_id, exc)
            failed += 1

    return synced, failed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync Oracle Integration Cloud integrations from source to target."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deployed without making any changes.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt and deploy immediately.",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    # Validate required env vars
    required = [
        "SOURCE_IDCS_HOST", "SOURCE_CLIENT_ID", "SOURCE_CLIENT_SECRET",
        "SOURCE_SCOPE", "SOURCE_OIC_HOST",
        "TARGET_IDCS_HOST", "TARGET_CLIENT_ID", "TARGET_CLIENT_SECRET",
        "TARGET_SCOPE", "TARGET_OIC_HOST",
    ]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        logger.error("Missing required environment variable(s): %s", ", ".join(missing))
        return 1

    source = OICClient(
        idcs_host=os.environ["SOURCE_IDCS_HOST"],
        client_id=os.environ["SOURCE_CLIENT_ID"],
        client_secret=os.environ["SOURCE_CLIENT_SECRET"],
        scope=os.environ["SOURCE_SCOPE"],
        oic_host=os.environ["SOURCE_OIC_HOST"],
        label="SOURCE",
    )
    target = OICClient(
        idcs_host=os.environ["TARGET_IDCS_HOST"],
        client_id=os.environ["TARGET_CLIENT_ID"],
        client_secret=os.environ["TARGET_CLIENT_SECRET"],
        scope=os.environ["TARGET_SCOPE"],
        oic_host=os.environ["TARGET_OIC_HOST"],
        label="TARGET",
    )

    activate_on_deploy = os.getenv("ACTIVATE_ON_DEPLOY", "true").strip().lower() == "true"
    integrations_file = os.getenv("INTEGRATIONS_FILE", "").strip() or None
    allowed_ids = load_integrations_file(integrations_file)

    mode = "DRY RUN" if args.dry_run else "SYNC"
    logger.info("=== OIC %s started ===", mode)
    logger.info("Source: %s", source.oic_host)
    logger.info("Target: %s", target.oic_host)
    logger.info("Activate on deploy: %s", activate_on_deploy)
    if allowed_ids is not None:
        logger.info("Filtering to %d integration(s) from file", len(allowed_ids))

    # --- Phase 1: plan ---
    pending, skipped = collect_pending(source, target, allowed_ids)
    print_plan(pending, activate_on_deploy)

    if args.dry_run:
        logger.info("=== Dry run complete — %d would be deployed, %d skipped ===", len(pending), skipped)
        return 0

    if not pending:
        logger.info("=== Nothing to deploy. %d skipped ===", skipped)
        return 0

    # --- Confirmation ---
    if not args.yes:
        try:
            answer = input(f"Deploy {len(pending)} integration(s) to {target.oic_host}? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print()
            logger.info("Aborted.")
            return 0
        if answer.strip().lower() != "y":
            logger.info("Aborted by user.")
            return 0

    # --- Phase 2: deploy ---
    synced, failed = deploy_pending(source, target, pending, activate_on_deploy)

    logger.info(
        "=== OIC Sync complete — synced: %d, skipped: %d, failed: %d ===",
        synced, skipped, failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
