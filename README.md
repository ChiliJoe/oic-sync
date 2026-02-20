# oic-sync

Sync Oracle Integration Cloud (OIC) integrations across environments.

---

## oic_sync.py

Downloads integration archives (.iar) from a **source** OIC environment and deploys them to a **target** OIC environment. Skips integrations that are already up to date in the target. Designed for unattended execution via cron.

### Prerequisites

- Python 3.10+
- `pip install -r requirements.txt`

### Setup

1. Copy `.env` and fill in the `SOURCE_*` and `TARGET_*` credential blocks:

    ```sh
    # Source environment
    SOURCE_IDCS_HOST=<idcs-host>
    SOURCE_CLIENT_ID=<client-id>
    SOURCE_CLIENT_SECRET=<client-secret>
    SOURCE_SCOPE=<scope>
    SOURCE_OIC_HOST=<oic-host>

    # Target environment
    TARGET_IDCS_HOST=<idcs-host>
    TARGET_CLIENT_ID=<client-id>
    TARGET_CLIENT_SECRET=<client-secret>
    TARGET_SCOPE=<scope>
    TARGET_OIC_HOST=<oic-host>
    ```

2. _(Optional)_ Set `INTEGRATIONS_FILE` to a file path listing specific integration IDs to sync. Leave it empty to sync all integrations.

3. _(Optional)_ Set `ACTIVATE_ON_DEPLOY=false` to skip activation after import (default: `true`).

### Usage

```sh
# Preview what would be deployed (no changes made)
python oic_sync.py --dry-run

# Deploy with confirmation prompt
python oic_sync.py

# Deploy immediately, skipping the prompt (for cron)
python oic_sync.py --yes
```

### CLI flags

| Flag | Description |
|---|---|
| `--dry-run` | Show what would be deployed without making any changes |
| `--yes`, `-y` | Skip the confirmation prompt (use in cron or CI) |

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SOURCE_IDCS_HOST` | Yes | — | IDCS host for the source environment |
| `SOURCE_CLIENT_ID` | Yes | — | OAuth client ID for source |
| `SOURCE_CLIENT_SECRET` | Yes | — | OAuth client secret for source |
| `SOURCE_SCOPE` | Yes | — | OAuth scope for source |
| `SOURCE_OIC_HOST` | Yes | — | OIC host for source |
| `TARGET_IDCS_HOST` | Yes | — | IDCS host for the target environment |
| `TARGET_CLIENT_ID` | Yes | — | OAuth client ID for target |
| `TARGET_CLIENT_SECRET` | Yes | — | OAuth client secret for target |
| `TARGET_SCOPE` | Yes | — | OAuth scope for target |
| `TARGET_OIC_HOST` | Yes | — | OIC host for target |
| `ACTIVATE_ON_DEPLOY` | No | `true` | Activate integration in target if it was ACTIVATED in source |
| `INTEGRATIONS_FILE` | No | _(empty)_ | Path to a file listing integration IDs to sync (one per line) |

### INTEGRATIONS_FILE format

One integration ID per line. Lines starting with `#` are ignored.

```
# Sync only these integrations
MY_INTEGRATION_A|01.00.0000
MY_INTEGRATION_B|02.01.0000
```

### Sync logic

For each integration in source:

1. If `INTEGRATIONS_FILE` is set and the integration is not listed — **skip**
2. Fetch integration details from target
3. If the integration exists in target and `target.lastUpdated >= source.lastUpdated` — **skip** (already up to date)
4. Download the archive from source
5. If the integration is ACTIVATED in target — deactivate it first
6. Import the archive (PUT to replace if it exists, POST to create if it does not)
7. If `ACTIVATE_ON_DEPLOY=true` and source status is ACTIVATED — activate in target

### Logging

Each run writes a timestamped log file (`oic-sync-YYYYMMDDHHMMSS.log`) alongside stdout output. The final line summarises the run:

```
=== OIC Sync complete — synced: 5, skipped: 12, failed: 0 ===
```

Exit code is `0` on success, `1` if any integration failed (suitable for cron alerting).

### Cron example

```cron
0 2 * * * /usr/bin/python3 /path/to/oic_sync.py --yes >> /var/log/oic_sync.log 2>&1
```
