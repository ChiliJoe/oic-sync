"""
oci_storage.py — OCI Object Storage helpers for OIC sync.

Uses Resource Principal authentication when running inside OCI (e.g. OCI
Functions). Falls back to a local OCI config file for development use.
"""


def upload(namespace: str, bucket: str, object_name: str, file_path: str) -> None:
    """Upload a local file to OCI Object Storage."""
    with open(file_path, "rb") as f:
        _client().put_object(namespace, bucket, object_name, f)


def download(namespace: str, bucket: str, object_name: str, local_path: str) -> None:
    """Download an OCI Object Storage object to a local path."""
    response = _client().get_object(namespace, bucket, object_name)
    with open(local_path, "wb") as f:
        for chunk in response.data.raw.stream(1024 * 1024, decode_content=False):
            f.write(chunk)


def _client():
    import oci  # type: ignore[import-untyped]
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        return oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    except Exception:
        config = oci.config.from_file()
        return oci.object_storage.ObjectStorageClient(config)
