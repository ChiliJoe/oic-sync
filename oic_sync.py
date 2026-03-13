#!/usr/bin/env python3
"""
oic_sync.py — Sync Oracle Integration Cloud integrations from source to target.

Can be run as a CLI script or imported by func.py for OCI Function deployment.

CLI usage:
    python oic_sync.py [--dry-run] [--yes] [--activate] [--background] [--no-verify-ssl]

    --dry-run          Show what would be deployed without making any changes.
    --yes, -y          Skip the confirmation prompt and deploy immediately.
    --activate         Activate deployed integrations to match source status.
    --background       Suppress progress bars (for cron/CI).
    --no-verify-ssl    Disable SSL certificate verification (default: enabled).

Environment variables (loaded from .env):
    SOURCE_IDCS_HOST, SOURCE_CLIENT_ID, SOURCE_CLIENT_SECRET, SOURCE_SCOPE, SOURCE_OIC_HOST
    TARGET_IDCS_HOST, TARGET_CLIENT_ID, TARGET_CLIENT_SECRET, TARGET_SCOPE, TARGET_OIC_HOST
    ACTIVATE_ON_DEPLOY  (true/false, default: false)
    DRY_RUN             (true/false, default: false) — alternative to --dry-run flag
    VERIFY_SSL          (true/false, default: true)  — alternative to --no-verify-ssl flag
    INTEGRATIONS_FILE   (optional path to file with integration IDs to sync)
    EXCLUSION_FILE      (optional path to file with integration IDs to exclude)
    OUTPUT_DIR          (directory for log and plan files, default: current directory)
"""

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests
from dotenv import load_dotenv
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from oic_client import OICClient  # noqa: F401 — BearerAuthSession also lives there

# ---------------------------------------------------------------------------
# Logging — module-level logger only; handlers are attached per run
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)


def _setup_logging(output_dir: str = ".") -> tuple[str, str]:
    """
    Attach logging handlers for this run.

    Returns (log_file, plan_file) paths inside output_dir.
    Safe to call once per process — avoids duplicate stream handlers.
    """
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    log_file = os.path.join(output_dir, f"oic-sync-{ts}.log")
    plan_file = os.path.join(output_dir, f"sync-plan-{ts}.txt")

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # Add stream handler only once (avoid duplicates on repeated calls)
    has_stream = any(
        type(h) is logging.StreamHandler for h in logger.handlers
    )
    if not has_stream:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return log_file, plan_file



# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def load_integrations_file(path: str | None) -> list[str] | None:
    """Load allowed integration IDs from a file. Returns None if no file configured.

    Preserves line order (used as deployment sequence) and deduplicates,
    keeping the first occurrence of any repeated ID.
    """
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"INTEGRATIONS_FILE not found: {path}")
    seen: set[str] = set()
    ids: list[str] = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line not in seen:
            ids.append(line)
            seen.add(line)
    logger.info("Loaded %d integration ID(s) from %s", len(ids), path)
    return ids


def load_exclusion_file(path: str | None) -> set[str] | None:
    """Load excluded integration IDs from a file. Returns None if no file configured."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"EXCLUSION_FILE not found: {path}")
    ids: set[str] = set()
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.add(line)
    logger.info("Loaded %d exclusion ID(s) from %s", len(ids), path)
    return ids


# ---------------------------------------------------------------------------
# Core sync logic — phase 1: planning
# ---------------------------------------------------------------------------

def collect_pending(
    source: OICClient,
    target: OICClient,
    allowed_ids: list[str] | None,
    excluded_ids: set[str] | None = None,
    show_progress: bool = True,
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
    allowed_set = set(allowed_ids) if allowed_ids is not None else None

    with logging_redirect_tqdm():
        for integration in tqdm(source_integrations, desc="Planning", unit="integration", disable=not show_progress):
            int_id = integration["id"]
            source_status = integration.get("status", "CONFIGURED")
            source_ts = integration.get("lastUpdated", "")

            if source_status not in ("ACTIVATED", "CONFIGURED"):
                logger.debug("SKIP [%s] — unsupported status: %s", int_id, source_status)
                skipped += 1
                continue

            if allowed_set is not None and int_id not in allowed_set:
                logger.debug("SKIP [%s] — not in integrations file", int_id)
                skipped += 1
                continue

            if excluded_ids is not None and int_id in excluded_ids:
                logger.debug("SKIP [%s] — in exclusion file", int_id)
                skipped += 1
                continue

            try:
                target_int = target.get_integration(int_id)
            except requests.HTTPError as exc:
                logger.warning("SKIP [%s] — could not query target: %s", int_id, exc)
                skipped += 1
                continue
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

    if allowed_ids is not None and pending:
        order = {id_: i for i, id_ in enumerate(allowed_ids)}
        pending.sort(key=lambda p: order.get(p["id"], len(allowed_ids)))
        logger.info("Deployment order follows INTEGRATIONS_FILE sequence")

    return pending, skipped


def print_plan(pending: list[dict], activate_on_deploy: bool, plan_file: str | None = None) -> None:
    """Print a human-readable table of integrations to be deployed."""
    lines = []
    if not pending:
        lines.append("\nNothing to deploy.")
    else:
        col_id = max(len(p["id"]) for p in pending)
        col_id = max(col_id, len("INTEGRATION ID"))
        header = f"  {'INTEGRATION ID':<{col_id}}  {'SOURCE':<10}  {'TARGET':<10}  ACTION"
        lines.append(f"\nIntegrations to deploy ({len(pending)}):")
        lines.append(f"  {'-' * (len(header) - 2)}")
        lines.append(header)
        lines.append(f"  {'-' * (len(header) - 2)}")
        for p in pending:
            target_col = p["target_status"] or "not found"
            action = p["action"] if activate_on_deploy else p["action"].replace(", then ACTIVATE", "")
            lines.append(f"  {p['id']:<{col_id}}  {p['source_status']:<10}  {target_col:<10}  {action}")
        lines.append("")

    output = "\n".join(lines)
    print(output)

    if plan_file:
        with open(plan_file, "w", encoding="utf-8") as f:
            f.write(output + "\n")


# ---------------------------------------------------------------------------
# Core sync logic — phase 2: deployment
# ---------------------------------------------------------------------------

def deploy_pending(
    source: OICClient,
    target: OICClient,
    pending: list[dict],
    activate_on_deploy: bool,
    show_progress: bool = True,
) -> tuple[int, int]:
    """
    Deploy all pending integrations.

    Returns (synced, failed) counts.
    """
    synced = failed = 0

    with logging_redirect_tqdm():
        for item in tqdm(pending, desc="Deploying", unit="integration", disable=not show_progress):
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
# run_sync — callable from both main() and func.py
# ---------------------------------------------------------------------------

def run_sync(
    *,
    source_idcs_host: str,
    source_client_id: str,
    source_client_secret: str,
    source_scope: str,
    source_oic_host: str,
    target_idcs_host: str,
    target_client_id: str,
    target_client_secret: str,
    target_scope: str,
    target_oic_host: str,
    activate_on_deploy: bool = False,
    dry_run: bool = False,
    verify_ssl: bool = True,
    integrations_file: str | None = None,
    exclusion_file: str | None = None,
    show_progress: bool = True,
    output_dir: str = ".",
    confirm_deploy: Callable[[int, str], bool] | None = None,
) -> dict:
    """
    Run a full OIC sync and return a result dict.

    confirm_deploy: optional callable(pending_count, target_host) -> bool.
        Called before deployment when pending integrations exist. If it returns
        False the run is aborted without deploying. Pass None to skip confirmation
        (always deploy).

    Returns:
        {
            "status": "ok" | "failed" | "dry_run" | "aborted" | "nothing_to_deploy",
            "synced": int, "skipped": int, "failed": int, "pending": int,
            "log_file": str, "plan_file": str,
        }
    """
    log_file, plan_file = _setup_logging(output_dir)

    source = OICClient(
        idcs_host=source_idcs_host,
        client_id=source_client_id,
        client_secret=source_client_secret,
        scope=source_scope,
        oic_host=source_oic_host,
        label="SOURCE",
        verify_ssl=verify_ssl,
    )
    target = OICClient(
        idcs_host=target_idcs_host,
        client_id=target_client_id,
        client_secret=target_client_secret,
        scope=target_scope,
        oic_host=target_oic_host,
        label="TARGET",
        verify_ssl=verify_ssl,
    )

    allowed_ids = load_integrations_file(integrations_file)
    excluded_ids = load_exclusion_file(exclusion_file)

    mode = "DRY RUN" if dry_run else "SYNC"
    logger.info("=== OIC %s started ===", mode)
    logger.info("Source: %s", source.oic_host)
    logger.info("Target: %s", target.oic_host)
    logger.info("Activate on deploy: %s", activate_on_deploy)
    logger.info("SSL verification: %s", verify_ssl)
    if allowed_ids is not None:
        logger.info("Filtering to %d integration(s) from file", len(allowed_ids))
    if excluded_ids is not None:
        logger.info("Excluding %d integration(s) from file", len(excluded_ids))

    # --- Phase 1: plan ---
    pending, skipped = collect_pending(
        source, target, allowed_ids,
        excluded_ids=excluded_ids,
        show_progress=show_progress,
    )
    print_plan(pending, activate_on_deploy, plan_file=plan_file)
    logger.info("Sync plan written to %s", plan_file)

    base_result = {"skipped": skipped, "pending": len(pending), "log_file": log_file, "plan_file": plan_file}

    if dry_run:
        logger.info("=== Dry run complete — %d would be deployed, %d skipped ===", len(pending), skipped)
        return {**base_result, "status": "dry_run", "synced": 0, "failed": 0}

    if not pending:
        logger.info("=== Nothing to deploy. %d skipped ===", skipped)
        return {**base_result, "status": "nothing_to_deploy", "synced": 0, "failed": 0}

    if confirm_deploy is not None and not confirm_deploy(len(pending), target.oic_host):
        logger.info("Aborted.")
        return {**base_result, "status": "aborted", "synced": 0, "failed": 0}

    # --- Phase 2: deploy ---
    synced, failed = deploy_pending(source, target, pending, activate_on_deploy, show_progress=show_progress)
    logger.info(
        "=== OIC Sync complete — synced: %d, skipped: %d, failed: %d ===",
        synced, skipped, failed,
    )
    return {**base_result, "status": "failed" if failed else "ok", "synced": synced, "failed": failed}


# ---------------------------------------------------------------------------
# CLI entry point
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
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Activate deployed integrations to match source status (default: off).",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()

    required = [
        "SOURCE_IDCS_HOST", "SOURCE_CLIENT_ID", "SOURCE_CLIENT_SECRET",
        "SOURCE_SCOPE", "SOURCE_OIC_HOST",
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
    activate_on_deploy = args.activate or os.getenv("ACTIVATE_ON_DEPLOY", "false").strip().lower() == "true"
    output_dir = os.getenv("OUTPUT_DIR", ".").strip() or "."

    def _prompt(n: int, host: str) -> bool:
        try:
            answer = input(f"Deploy {n} integration(s) to {host}? [y/N] ")
            return answer.strip().lower() == "y"
        except (EOFError, KeyboardInterrupt):
            print()
            return False

    confirm_fn: Callable[[int, str], bool] | None = None if args.yes else _prompt

    try:
        result = run_sync(
            source_idcs_host=os.environ["SOURCE_IDCS_HOST"],
            source_client_id=os.environ["SOURCE_CLIENT_ID"],
            source_client_secret=os.environ["SOURCE_CLIENT_SECRET"],
            source_scope=os.environ["SOURCE_SCOPE"],
            source_oic_host=os.environ["SOURCE_OIC_HOST"],
            target_idcs_host=os.environ["TARGET_IDCS_HOST"],
            target_client_id=os.environ["TARGET_CLIENT_ID"],
            target_client_secret=os.environ["TARGET_CLIENT_SECRET"],
            target_scope=os.environ["TARGET_SCOPE"],
            target_oic_host=os.environ["TARGET_OIC_HOST"],
            activate_on_deploy=activate_on_deploy,
            dry_run=dry_run,
            verify_ssl=verify_ssl,
            integrations_file=os.getenv("INTEGRATIONS_FILE", "").strip() or None,
            exclusion_file=os.getenv("EXCLUSION_FILE", "").strip() or None,
            show_progress=not args.background,
            output_dir=output_dir,
            confirm_deploy=confirm_fn,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
