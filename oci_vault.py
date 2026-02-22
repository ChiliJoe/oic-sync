"""
oci_vault.py — OCI Vault helpers for OIC sync.

Uses Resource Principal authentication when running inside OCI (e.g. OCI
Functions). Falls back to a local OCI config file for development use.
"""

import base64


def is_vault_ocid(value: str) -> bool:
    """Return True if value looks like an OCI Vault secret OCID."""
    return value.startswith("ocid1.vaultsecret.")


def fetch_secret(secret_ocid: str) -> str:
    """Fetch and return the plaintext value of an OCI Vault secret."""
    response = _client().get_secret_bundle(secret_ocid)
    content = response.data.secret_bundle_content
    return base64.b64decode(content.content).decode("utf-8")


def _client():
    import oci  # type: ignore[import-untyped]
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        return oci.secrets.SecretsClient(config={}, signer=signer)
    except Exception:
        config = oci.config.from_file()
        return oci.secrets.SecretsClient(config)
