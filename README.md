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

3. _(Optional)_ Set `ACTIVATE_ON_DEPLOY=true` to activate integrations after import (default: `false`). Alternatively, pass `--activate` at runtime.

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
| --- | --- |
| `--dry-run` | Show what would be deployed without making any changes |
| `--yes`, `-y` | Skip the confirmation prompt (use in cron or CI) |
| `--activate` | Activate deployed integrations to match source status (default: off) |
| `--background` | Suppress progress bars (use in cron or CI) |
| `--no-verify-ssl` | Disable SSL certificate verification (default: enabled) |

### Environment Variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
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
| `ACTIVATE_ON_DEPLOY` | No | `false` | Activate integration in target if it was ACTIVATED in source |
| `DRY_RUN` | No | `false` | Preview changes without deploying (env var alternative to `--dry-run`) |
| `VERIFY_SSL` | No | `true` | SSL certificate verification (`false` to disable) |
| `INTEGRATIONS_FILE` | No | _(empty)_ | Path to a file listing integration IDs to sync (one per line) |
| `EXCLUSION_FILE` | No | _(empty)_ | Path to a file listing integration IDs to exclude from sync (one per line) |
| `OUTPUT_DIR` | No | `.` | Directory for log and plan output files |

### INTEGRATIONS_FILE format

One integration ID per line. Lines starting with `#` are ignored. Duplicate entries are silently deduplicated (first occurrence wins).

```text
# Sync only these integrations — deployed in this order
MY_INTEGRATION_A|01.00.0000
MY_INTEGRATION_B|02.01.0000
```

When `INTEGRATIONS_FILE` is set, integrations are deployed in the order they appear in the file. Use this to control dependency sequencing (e.g. deploy a shared connection before the integrations that depend on it).

### EXCLUSION_FILE format

One integration ID per line. Lines starting with `#` are ignored.

```text
# Never sync these integrations
MY_TEST_INTEGRATION|01.00.0000
MY_DEPRECATED_FLOW|03.00.0000
```

When both `INTEGRATIONS_FILE` and `EXCLUSION_FILE` are set, the exclusion list is applied after the inclusion filter — an integration must be in the inclusion list AND not in the exclusion list to be synced.

### Sync logic

For each integration in source:

1. If `INTEGRATIONS_FILE` is set and the integration is not listed — **skip**
2. Fetch integration details from target
3. If the integration exists in target and `target.lastUpdated >= source.lastUpdated` — **skip** (already up to date)
4. Download the archive from source
5. If the integration is ACTIVATED in target — deactivate it first
6. Import the archive (PUT to replace if it exists, POST to create if it does not)
7. If `--activate` (or `ACTIVATE_ON_DEPLOY=true`) and source status is ACTIVATED — activate in target

### Output files

Each run writes two timestamped files to the working directory:

- **`oic-sync-YYYYMMDDHHMMSS.log`** — full run log including stdout output. The final line summarises the run:

  ```text
  === OIC Sync complete — synced: 5, skipped: 12, failed: 0 ===
  ```

- **`sync-plan-YYYYMMDDHHMMSS.txt`** — the sync plan table showing which integrations would be (or were) deployed, their source/target statuses, and the action taken.

Exit code is `0` on success, `1` if any integration failed (suitable for cron alerting).

### Cron example

```cron
0 2 * * * /usr/bin/python3 /path/to/oic_sync.py --yes --background >> /var/log/oic_sync.log 2>&1
```

---

## OCI Function deployment

### Requirements

- [OCI CLI](https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm) and [Fn CLI](https://fnproject.io/tutorials/install/) installed and configured
- An OCI Function application already created

### Deploy

```sh
fn deploy --app <your-app-name>
```

### Configuration items

Set the following via the OCI Console (**Functions → your app → your function → Configuration**) or via the Fn CLI:

All variables from the table above apply. Key ones for OCI Function context:

| Variable | Notes |
| --- | --- |
| `SOURCE_*` / `TARGET_*` | Required — same as local usage |
| `DRY_RUN` | Set to `true` for a safe first test invocation |
| `VERIFY_SSL` | Defaults to `true`; set to `false` only if needed |
| `INTEGRATIONS_FILE` | Object name in the OCI bucket (e.g. `my-integrations.txt`) — downloaded to `/tmp/` before sync. Requires `OCI_BUCKET_NAME` + `OCI_NAMESPACE`. |
| `EXCLUSION_FILE` | Object name in the OCI bucket (e.g. `exclusions.txt`) — downloaded to `/tmp/` before sync. Requires `OCI_BUCKET_NAME` + `OCI_NAMESPACE`. |
| `OCI_NAMESPACE` + `OCI_BUCKET_NAME` | Required when `INTEGRATIONS_FILE` or `EXCLUSION_FILE` is set. Also used to upload log and plan files after each run. |
| `OUTPUT_DIR` | Defaults to `/tmp` in the function (set automatically by `func.py`) |

### Invoke

```sh
fn invoke <your-app-name> oic-sync
```

The function returns a JSON response:

```json
{
  "status": "ok",
  "synced": 5,
  "skipped": 12,
  "failed": 0,
  "pending": 5,
  "log_file": "/tmp/oic-sync-20260222120000.log",
  "plan_file": "/tmp/sync-plan-20260222120000.txt"
}
```

Possible `status` values: `ok`, `failed`, `dry_run`, `nothing_to_deploy`, `aborted`, `error`.
