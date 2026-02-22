"""
func.py — OCI Function entry point for oic_sync.

All configuration is read from OCI Function configuration items (environment
variables). No CLI flags, no interactive prompts, no .env file loading.

Required configuration items:
    SOURCE_IDCS_HOST, SOURCE_CLIENT_ID, SOURCE_CLIENT_SECRET, SOURCE_SCOPE, SOURCE_OIC_HOST
    TARGET_IDCS_HOST, TARGET_CLIENT_ID, TARGET_CLIENT_SECRET, TARGET_SCOPE, TARGET_OIC_HOST

    SOURCE_CLIENT_SECRET / TARGET_CLIENT_SECRET may also be an OCI Vault secret
    OCID (ocid1.vaultsecret...) — the plaintext secret is fetched at runtime.

Optional configuration items:
    ACTIVATE_ON_DEPLOY   (true/false, default: false)
    DRY_RUN              (true/false, default: false)
    VERIFY_SSL           (true/false, default: true)
    INTEGRATIONS_FILE    (object name in OCI bucket — downloaded to /tmp/ before sync)
    EXCLUSION_FILE       (object name in OCI bucket — downloaded to /tmp/ before sync)
    OCI_NAMESPACE        (Object Storage namespace — required when INTEGRATIONS_FILE or
                          EXCLUSION_FILE is set; also used for log/plan upload)
    OCI_BUCKET_NAME      (Object Storage bucket — required when INTEGRATIONS_FILE or
                          EXCLUSION_FILE is set; also used for log/plan upload)
"""

import io
import json
import os

import fdk.response as fdk_response  # type: ignore[import-untyped]

import oci_storage
import oci_vault
import oic_sync

_REQUIRED_VARS = [
    "SOURCE_IDCS_HOST", "SOURCE_CLIENT_ID", "SOURCE_CLIENT_SECRET",
    "SOURCE_SCOPE", "SOURCE_OIC_HOST",
    "TARGET_IDCS_HOST", "TARGET_CLIENT_ID", "TARGET_CLIENT_SECRET",
    "TARGET_SCOPE", "TARGET_OIC_HOST",
]


def handler(ctx, data: io.BytesIO = None): # pyright: ignore[reportArgumentType]
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    if missing:
        body = {"status": "error", "error": f"Missing configuration item(s): {missing}"}
        return fdk_response.Response(
            ctx,
            response_data=json.dumps(body),
            headers={"Content-Type": "application/json"},
            status_code=400,
        )

    integrations_obj = os.getenv("INTEGRATIONS_FILE", "").strip() or None
    exclusion_obj    = os.getenv("EXCLUSION_FILE", "").strip() or None
    bucket           = os.getenv("OCI_BUCKET_NAME", "").strip() or None
    namespace        = os.getenv("OCI_NAMESPACE", "").strip() or None

    if (integrations_obj or exclusion_obj) and not (bucket and namespace):
        body = {
            "status": "error",
            "error": (
                "OCI_BUCKET_NAME and OCI_NAMESPACE are required when "
                "INTEGRATIONS_FILE or EXCLUSION_FILE is set"
            ),
        }
        return fdk_response.Response(
            ctx,
            response_data=json.dumps(body),
            headers={"Content-Type": "application/json"},
            status_code=400,
        )

    try:
        integrations_file: str | None = None
        exclusion_file: str | None = None
        if integrations_obj and namespace and bucket:
            integrations_file = f"/tmp/{integrations_obj}"
            oci_storage.download(namespace, bucket, integrations_obj, integrations_file)
        if exclusion_obj and namespace and bucket:
            exclusion_file = f"/tmp/{exclusion_obj}"
            oci_storage.download(namespace, bucket, exclusion_obj, exclusion_file)

        src_secret = os.environ["SOURCE_CLIENT_SECRET"]
        if oci_vault.is_vault_ocid(src_secret):
            src_secret = oci_vault.fetch_secret(src_secret)

        tgt_secret = os.environ["TARGET_CLIENT_SECRET"]
        if oci_vault.is_vault_ocid(tgt_secret):
            tgt_secret = oci_vault.fetch_secret(tgt_secret)

        result = oic_sync.run_sync(
            source_idcs_host=os.environ["SOURCE_IDCS_HOST"],
            source_client_id=os.environ["SOURCE_CLIENT_ID"],
            source_client_secret=src_secret,
            source_scope=os.environ["SOURCE_SCOPE"],
            source_oic_host=os.environ["SOURCE_OIC_HOST"],
            target_idcs_host=os.environ["TARGET_IDCS_HOST"],
            target_client_id=os.environ["TARGET_CLIENT_ID"],
            target_client_secret=tgt_secret,
            target_scope=os.environ["TARGET_SCOPE"],
            target_oic_host=os.environ["TARGET_OIC_HOST"],
            activate_on_deploy=os.getenv("ACTIVATE_ON_DEPLOY", "false").lower() == "true",
            dry_run=os.getenv("DRY_RUN", "false").lower() == "true",
            verify_ssl=os.getenv("VERIFY_SSL", "true").lower() == "true",
            integrations_file=integrations_file,
            exclusion_file=exclusion_file,
            show_progress=False,  # no TTY in function context
            output_dir="/tmp",
            confirm_deploy=None,  # no interactive prompt in function context
        )

        if bucket and namespace:
            for key in ("log_file", "plan_file"):
                file_path = result.get(key, "")
                if file_path and os.path.exists(file_path):
                    object_name = os.path.basename(file_path)
                    oci_storage.upload(namespace, bucket, object_name, file_path)
                    result[key] = f"oci://{namespace}/{bucket}/{object_name}"

        return fdk_response.Response(
            ctx,
            response_data=json.dumps(result),
            headers={"Content-Type": "application/json"},
        )

    except Exception as exc:
        body = {"status": "error", "error": str(exc)}
        return fdk_response.Response(
            ctx,
            response_data=json.dumps(body),
            headers={"Content-Type": "application/json"},
            status_code=500,
        )
