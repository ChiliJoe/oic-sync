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
python oic_sync.py              # interactive (prompts before deploying)
python oic_sync.py --dry-run    # preview changes without deploying
python oic_sync.py --yes        # deploy without confirmation (for cron/CI)
```

There are no automated tests. Use `--dry-run` to validate behavior against real OIC environments.

## Architecture

Single-file application: `oic_sync.py` (~430 lines). No build step.

**Core class: `OICClient`** (lines 55–163)
Wraps the OIC REST API for one environment. Handles OAuth2 token refresh, pagination, archive download/upload, and activation state management.

**Two-phase sync flow in `main()`:**
1. **Planning** (`collect_pending()`) — Compares source vs. target integrations by `lastUpdated` timestamp. Builds a list of integrations that need deployment. Respects an optional whitelist file (`INTEGRATIONS_FILE` env var).
2. **Deployment** (`deploy_pending()`) — Downloads `.iar` archives from source, deactivates if needed, imports to target, then reactivates to match source state.

**Key implementation details:**
- Integration IDs contain `|` which must be URL-encoded as `%7C` in API paths
- HTTP PATCH is sent via POST with `X-HTTP-Method-Override: PATCH` header
- Import uses `POST` for new integrations, `PUT` (via override) for updates
- SSL verification is disabled (`verify=False`) — expected for OIC environments
- OAuth2 tokens have a 30-second expiry buffer in `_refresh_token()`
- Timeouts: 30s for most calls, 120s for archive download/upload
- Log files written to `oic-sync-YYYYMMDDHHMMSS.log` in the working directory
- Exit code: 0 = success (including skips), 1 = any failures occurred
