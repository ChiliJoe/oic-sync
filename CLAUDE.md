# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Setup:**
```bash
pip install -r requirements.txt
cp env_sample .env  # then fill in credentials
```

**Run:**
```bash
python oic_sync.py                        # interactive with progress bars
python oic_sync.py --dry-run              # preview changes without deploying
python oic_sync.py --yes --background     # deploy without confirmation or progress bars (for cron/CI)
python oic_sync.py --no-verify-ssl        # disable SSL certificate verification
```

There are no automated tests. Use `--dry-run` to validate behavior against real OIC environments.

## Architecture

Single-file application: `oic_sync.py` (~470 lines). No build step. Dependencies: `requests`, `python-dotenv`, `tqdm`.

**`BearerAuthSession`** (before `OICClient`)
Subclass of `requests.Session` that injects `Authorization: Bearer` on every request — including redirect targets — replicating `curl --location-trusted`. SSL verification and TLS warnings are controlled per-instance via `verify_ssl`.

**Core class: `OICClient`**
Wraps the OIC REST API for one environment. Holds a `BearerAuthSession`, manages OAuth2 token refresh (`_ensure_token()` / `_refresh_token()`), and provides methods for listing, downloading, importing, and activating integrations. `_check_response()` parses OIC JSON error bodies (`detail`, `title`, `message`) into readable `HTTPError` messages.

**Two-phase sync flow in `main()`:**
1. **Planning** (`collect_pending()`) — Compares source vs. target integrations by `lastUpdated` timestamp. Per-integration target errors are caught and skipped (warning logged) rather than aborting the run. Shows a tqdm progress bar unless `--background`.
2. **Deployment** (`deploy_pending()`) — Downloads `.iar` archives from source, deactivates if needed, imports to target, then reactivates to match source state. Also shows a tqdm progress bar unless `--background`.

**Key implementation details:**

- Integration IDs contain `|` which must be URL-encoded as `%7C` in API paths
- HTTP PATCH is sent via POST with `X-HTTP-Method-Override: PATCH` header
- Import uses `POST` for new integrations, `PUT` (via override) for updates
- SSL verification is enabled by default; disable with `--no-verify-ssl`
- `urllib3` InsecureRequestWarning is suppressed automatically when `verify_ssl=False`
- OAuth2 tokens have a 30-second expiry buffer in `_refresh_token()`
- Timeouts: 30s for most calls, 120s for archive download/upload
- Log files written to `oic-sync-YYYYMMDDHHMMSS.log` in the working directory
- Plan file written to `sync-plan-YYYYMMDDHHMMSS.txt` in the working directory (mirrors the table printed to stdout)
- Exit code: 0 = success (including skips), 1 = any failures occurred
