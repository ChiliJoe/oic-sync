#!/usr/bin/env python3
"""
clear_oic.py — Delete all integrations, connections, lookups, and packages
from the TARGET Oracle Integration Cloud environment.

WARNING: This is a destructive operation. Use --dry-run to preview first.

CLI usage:
    python clear_oic.py [--dry-run] [--yes] [--background] [--no-verify-ssl]

    --dry-run          Show what would be deleted without making any changes.
    --yes, -y          Skip the confirmation prompt and delete immediately.
    --background       Suppress progress bars (for cron/CI).
    --no-verify-ssl    Disable SSL certificate verification (default: enabled).

Environment variables (loaded from .env):
    TARGET_IDCS_HOST, TARGET_CLIENT_ID, TARGET_CLIENT_SECRET, TARGET_SCOPE, TARGET_OIC_HOST
    DRY_RUN             (true/false, default: false) — alternative to --dry-run flag
    VERIFY_SSL          (true/false, default: true)  — alternative to --no-verify-ssl flag
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from oic_client import OICClient

# ---------------------------------------------------------------------------
# Logging — module-level logger only; handlers are attached per run
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Attach a stream handler for this run (once only)."""
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    has_stream = any(type(h) is logging.StreamHandler for h in logger.handlers)
    if not has_stream:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)


# ---------------------------------------------------------------------------
# Core clear logic
# ---------------------------------------------------------------------------

def _delete_integrations(client: OICClient, integrations: list[dict], show_progress: bool) -> tuple[int, int]:
    """Deactivate then delete each integration. Returns (deleted, failed)."""
    deleted = failed = 0
    with logging_redirect_tqdm():
        for item in tqdm(integrations, desc="Deleting integrations", unit="integration", disable=not show_progress):
            int_id = item["id"]
            status = item.get("status", "CONFIGURED")
            try:
                if status == "ACTIVATED":
                    logger.info("Deactivating [%s]...", int_id)
                    client.deactivate_integration(int_id)
                logger.info("Deleting integration [%s]...", int_id)
                client.delete_integration(int_id)
                deleted += 1
            except Exception as exc:
                logger.warning("SKIP [%s] — %s", int_id, exc)
                failed += 1
    return deleted, failed


def _delete_connections(client: OICClient, connections: list[dict], show_progress: bool) -> tuple[int, int]:
    """Delete each connection. Returns (deleted, failed)."""
    deleted = failed = 0
    with logging_redirect_tqdm():
        for item in tqdm(connections, desc="Deleting connections", unit="connection", disable=not show_progress):
            conn_id = item.get("id") or item.get("name", "")
            try:
                logger.info("Deleting connection [%s]...", conn_id)
                client.delete_connection(conn_id)
                deleted += 1
            except Exception as exc:
                logger.warning("SKIP connection [%s] — %s", conn_id, exc)
                failed += 1
    return deleted, failed


def _delete_lookups(client: OICClient, lookups: list[dict], show_progress: bool) -> tuple[int, int]:
    """Delete each lookup. Returns (deleted, failed)."""
    deleted = failed = 0
    with logging_redirect_tqdm():
        for item in tqdm(lookups, desc="Deleting lookups", unit="lookup", disable=not show_progress):
            name = item.get("name", "")
            try:
                logger.info("Deleting lookup [%s]...", name)
                client.delete_lookup(name)
                deleted += 1
            except Exception as exc:
                logger.warning("SKIP lookup [%s] — %s", name, exc)
                failed += 1
    return deleted, failed


def _delete_packages(client: OICClient, packages: list[dict], show_progress: bool) -> tuple[int, int]:
    """Delete each package. Returns (deleted, failed)."""
    deleted = failed = 0
    with logging_redirect_tqdm():
        for item in tqdm(packages, desc="Deleting packages", unit="package", disable=not show_progress):
            name = item.get("name", "")
            try:
                logger.info("Deleting package [%s]...", name)
                client.delete_package(name)
                deleted += 1
            except Exception as exc:
                logger.warning("SKIP package [%s] — %s", name, exc)
                failed += 1
    return deleted, failed


def _delete_libraries(client: OICClient, libraries: list[dict], show_progress: bool) -> tuple[int, int]:
    """Delete each library. Returns (deleted, failed)."""
    deleted = failed = 0
    with logging_redirect_tqdm():
        for item in tqdm(libraries, desc="Deleting libraries", unit="library", disable=not show_progress):
            lib_id = item.get("id", "")
            try:
                logger.info("Deleting library [%s]...", lib_id)
                client.delete_library(lib_id)
                deleted += 1
            except Exception as exc:
                logger.warning("SKIP library [%s] — %s", lib_id, exc)
                failed += 1
    return deleted, failed


def run_clear(
    client: OICClient,
    dry_run: bool = False,
    yes: bool = False,
    show_progress: bool = True,
) -> dict:
    """
    Collect all resources on the target environment and delete them.

    Returns a result dict:
        {
            "status": "ok" | "failed" | "dry_run" | "aborted" | "nothing_to_delete",
            "integrations_deleted": int, "libraries_deleted": int, "connections_deleted": int,
            "lookups_deleted": int, "packages_deleted": int,
            "failed": int,
        }
    """
    mode = "DRY RUN" if dry_run else "CLEAR"
    logger.info("=== OIC %s started ===", mode)
    logger.info("Target: %s", client.oic_host)

    # --- Collect ---
    logger.info("Listing integrations...")
    integrations = client.list_integrations()
    logger.info("Found %d integration(s).", len(integrations))

    logger.info("Listing connections...")
    connections = client.list_connections()
    logger.info("Found %d connection(s).", len(connections))

    logger.info("Listing lookups...")
    lookups = client.list_lookups()
    logger.info("Found %d lookup(s).", len(lookups))

    logger.info("Listing packages...")
    packages = client.list_packages()
    logger.info("Found %d package(s).", len(packages))

    logger.info("Listing libraries...")
    libraries = client.list_libraries()
    logger.info("Found %d library(ies).", len(libraries))

    total = len(integrations) + len(connections) + len(lookups) + len(packages) + len(libraries)

    # --- Summary ---
    col = 20
    print()
    print(f"  {'Resource':<{col}}  Count")
    print(f"  {'-' * col}  -----")
    print(f"  {'Integrations':<{col}}  {len(integrations)}")
    print(f"  {'Libraries':<{col}}  {len(libraries)}")
    print(f"  {'Connections':<{col}}  {len(connections)}")
    print(f"  {'Lookups':<{col}}  {len(lookups)}")
    print(f"  {'Packages':<{col}}  {len(packages)}")
    print()

    if total == 0:
        logger.info("=== Nothing to delete. ===")
        return {
            "status": "nothing_to_delete",
            "integrations_deleted": 0, "libraries_deleted": 0, "connections_deleted": 0,
            "lookups_deleted": 0, "packages_deleted": 0, "failed": 0,
        }

    if dry_run:
        print("[DRY RUN] No changes made.")
        logger.info("=== Dry run complete — %d resource(s) would be deleted ===", total)
        return {
            "status": "dry_run",
            "integrations_deleted": 0, "libraries_deleted": 0, "connections_deleted": 0,
            "lookups_deleted": 0, "packages_deleted": 0, "failed": 0,
        }

    # --- Confirm ---
    if not yes:
        print(f"WARNING: This will permanently delete {total} resource(s) from {client.oic_host}.")
        try:
            answer = input("Type 'yes' to confirm: ")
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer.strip().lower() != "yes":
            logger.info("Aborted.")
            return {
                "status": "aborted",
                "integrations_deleted": 0, "libraries_deleted": 0, "connections_deleted": 0,
                "lookups_deleted": 0, "packages_deleted": 0, "failed": 0,
            }

    # --- Delete (in order: integrations → libraries → connections → lookups → packages) ---
    int_deleted, int_failed = _delete_integrations(client, integrations, show_progress)
    lib_deleted, lib_failed = _delete_libraries(client, libraries, show_progress)
    con_deleted, con_failed = _delete_connections(client, connections, show_progress)
    lkp_deleted, lkp_failed = _delete_lookups(client, lookups, show_progress)
    pkg_deleted, pkg_failed = _delete_packages(client, packages, show_progress)

    total_failed = int_failed + lib_failed + con_failed + lkp_failed + pkg_failed

    # --- Report ---
    print()
    print(f"  {'Resource':<{col}}  Deleted  Failed")
    print(f"  {'-' * col}  -------  ------")
    print(f"  {'Integrations':<{col}}  {int_deleted:<7}  {int_failed}")
    print(f"  {'Libraries':<{col}}  {lib_deleted:<7}  {lib_failed}")
    print(f"  {'Connections':<{col}}  {con_deleted:<7}  {con_failed}")
    print(f"  {'Lookups':<{col}}  {lkp_deleted:<7}  {lkp_failed}")
    print(f"  {'Packages':<{col}}  {pkg_deleted:<7}  {pkg_failed}")
    print()

    logger.info(
        "=== OIC Clear complete — integrations: %d, libraries: %d, connections: %d, lookups: %d, packages: %d, failed: %d ===",
        int_deleted, lib_deleted, con_deleted, lkp_deleted, pkg_deleted, total_failed,
    )

    return {
        "status": "failed" if total_failed else "ok",
        "integrations_deleted": int_deleted,
        "libraries_deleted": lib_deleted,
        "connections_deleted": con_deleted,
        "lookups_deleted": lkp_deleted,
        "packages_deleted": pkg_deleted,
        "failed": total_failed,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete all integrations, connections, lookups, and packages from the TARGET OIC environment."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without making any changes.",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip the confirmation prompt and delete immediately.",
    )
    parser.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_false",
        default=True,
        help="Disable SSL certificate verification for all API calls (default: enabled).",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="Background/headless mode: suppress progress bars (default: off).",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    _setup_logging()
    args = parse_args()

    required = [
        "TARGET_IDCS_HOST", "TARGET_CLIENT_ID", "TARGET_CLIENT_SECRET",
        "TARGET_SCOPE", "TARGET_OIC_HOST",
    ]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"ERROR: Missing required environment variable(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    # CLI flags take priority; env vars provide defaults
    dry_run = args.dry_run or os.getenv("DRY_RUN", "false").strip().lower() == "true"
    verify_ssl = args.verify_ssl
    if os.getenv("VERIFY_SSL", "").strip().lower() == "false":
        verify_ssl = False

    client = OICClient(
        idcs_host=os.environ["TARGET_IDCS_HOST"],
        client_id=os.environ["TARGET_CLIENT_ID"],
        client_secret=os.environ["TARGET_CLIENT_SECRET"],
        scope=os.environ["TARGET_SCOPE"],
        oic_host=os.environ["TARGET_OIC_HOST"],
        label="TARGET",
        verify_ssl=verify_ssl,
    )

    result = run_clear(
        client,
        dry_run=dry_run,
        yes=args.yes,
        show_progress=not args.background,
    )

    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
