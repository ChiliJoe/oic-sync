# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Setup:**
```bash
pip install -r requirements.txt
cp env_sample .env  # then fill in credentials
```

**Run (CLI):**
```bash
python oic_sync.py                        # interactive with progress bars
python oic_sync.py --dry-run              # preview changes without deploying
python oic_sync.py --yes --background     # deploy without confirmation or progress bars (for cron/CI)
python oic_sync.py --no-verify-ssl        # disable SSL certificate verification
```

**Deploy as OCI Function:**
```bash
fn deploy --app <your-app-name>
fn invoke <your-app-name> oic-sync
```

There are no automated tests. Use `--dry-run` to validate behavior against real OIC environments.

## Architecture

Two entry points, shared core logic:

- **`oic_sync.py`** — all sync logic plus CLI entry point (`main()`). Can also be imported.
- **`func.py`** — thin OCI Function handler; reads config from env vars, downloads filter files from OCI bucket, calls `run_sync()`, uploads output files to OCI bucket.
- **`oci_storage.py`** — OCI Object Storage helpers (`upload`, `download`). Used only by `func.py`. Resource Principal auth first, falls back to OCI config file.
- **`func.yaml`** — OCI Function metadata (runtime, entrypoint, memory, timeout).

Dependencies: `requests`, `python-dotenv`, `tqdm`, `fdk` (OCI Functions), `oci` (Object Storage).

**`BearerAuthSession`** (before `OICClient`)
Subclass of `requests.Session` that injects `Authorization: Bearer` on every request — including redirect targets — replicating `curl --location-trusted`. SSL verification and TLS warnings are controlled per-instance via `verify_ssl`.

**Core class: `OICClient`**
Wraps the OIC REST API for one environment. Holds a `BearerAuthSession`, manages OAuth2 token refresh (`_ensure_token()` / `_refresh_token()`), and provides methods for listing, downloading, importing, and activating integrations. `_check_response()` parses OIC JSON error bodies (`detail`, `title`, `message`) into readable `HTTPError` messages.

**Two-phase sync flow via `run_sync()`:**

1. **Planning** (`collect_pending()`) — Compares source vs. target integrations by `lastUpdated` timestamp. Per-integration target errors are caught and skipped (warning logged) rather than aborting the run. Shows a tqdm progress bar unless `show_progress=False`.
2. **Deployment** (`deploy_pending()`) — Downloads `.iar` archives from source, deactivates if needed, imports to target, then reactivates to match source state.

`run_sync()` accepts a `confirm_deploy` callback for the interactive prompt (CLI) or `None` (function context). Returns a result dict with `status`, `synced`, `skipped`, `failed`, `log_file`, `plan_file`.

`main()` wraps `run_sync()` for CLI use: parses args, loads `.env`, handles confirmation prompt.

**`func.py` OCI Function flow:**

1. Validates required env vars; returns 400 on missing.
2. If `INTEGRATIONS_FILE` or `EXCLUSION_FILE` is set, validates that `OCI_BUCKET_NAME` + `OCI_NAMESPACE` are also set (returns 400 if not), then downloads the objects to `/tmp/` via `oci_storage.download()`.
3. Calls `run_sync()` with the local `/tmp/` paths.
4. If `OCI_BUCKET_NAME` + `OCI_NAMESPACE` are set, uploads log and plan files via `oci_storage.upload()`.

**Key implementation details:**

- Integration IDs contain `|` which must be URL-encoded as `%7C` in API paths
- HTTP PATCH is sent via POST with `X-HTTP-Method-Override: PATCH` header
- Import uses `POST` for new integrations, `PUT` (via override) for updates
- SSL verification is enabled by default; disable with `--no-verify-ssl` or `VERIFY_SSL=false`
- `urllib3` InsecureRequestWarning is suppressed automatically when `verify_ssl=False`
- OAuth2 tokens have a 30-second expiry buffer in `_refresh_token()`
- Timeouts: 30s for most calls, 120s for archive download/upload
- `EXCLUSION_FILE` env var: optional path to a file of integration IDs to skip; applied after `INTEGRATIONS_FILE` filter
- `_setup_logging(output_dir)` attaches handlers per run; avoids module-level file creation (safe to import)
- Log files written to `oic-sync-YYYYMMDDHHMMSS.log` in `OUTPUT_DIR` (default: working directory)
- Plan file written to `sync-plan-YYYYMMDDHHMMSS.txt` in `OUTPUT_DIR` (mirrors the table printed to stdout)
- Exit code: 0 = success (including skips), 1 = any failures occurred
