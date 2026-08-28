"""
R2 stream upload helper for Vercel Serverless.
"""
import os
import requests
import boto3
from boto3.s3.transfer import TransferConfig

def get_s3_client():
    """Initialize Boto3 client for Cloudflare R2."""
    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY")
    secret_key = os.environ.get("R2_SECRET_KEY")

    if not all([account_id, access_key, secret_key]):
        raise ValueError("Missing R2 credentials in environment variables")

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

def check_object_exists(bucket_name: str, key: str) -> bool:
    """Check if an object exists in R2."""
    client = get_s3_client()
    try:
        client.head_object(Bucket=bucket_name, Key=key)
        return True
    except Exception:
        return False

def delete_object(bucket_name: str, key: str) -> bool:
    """Delete a specific object from R2."""
    client = get_s3_client()
    try:
        client.delete_object(Bucket=bucket_name, Key=key)
        return True
    except Exception:
        return False

def delete_objects_by_prefix(bucket_name: str, prefix: str) -> list[str]:
    """Delete all objects matching a prefix. Returns list of deleted keys."""
    client = get_s3_client()
    deleted_keys = []
    
    paginator = client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        if 'Contents' not in page:
            continue
            
        objects_to_delete = [{'Key': obj['Key']} for obj in page['Contents']]
        if objects_to_delete:
            client.delete_objects(
                Bucket=bucket_name,
                Delete={'Objects': objects_to_delete, 'Quiet': True}
            )
            deleted_keys.extend([obj['Key'] for obj in objects_to_delete])
            
    return deleted_keys

def generate_presigned_url(bucket_name: str, key: str, expires_in: int = 3600) -> str:
    """Generate a pre-signed download URL for R2."""
    client = get_s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket_name, "Key": key},
        ExpiresIn=expires_in
    )

def stream_upload_to_r2(audio_url: str, bucket_name: str, object_key: str, content_type: str = "audio/mp4") -> None:
    """
    Download from audio URL to a temporary file, then upload to R2.
    This avoids boto3/requests pipeline deadlocks while still keeping
    memory usage low. The /tmp directory on Vercel provides 512MB.
    """
    client = get_s3_client()
    import tempfile
    
    # Download to a temporary file
    with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
        tmp_filename = tmp_file.name
        with requests.get(audio_url, stream=True, timeout=30) as r:
            r.raise_for_status()
            for chunk in r.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
                
    # Upload to R2
    try:
        config = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,
            max_concurrency=2,
            multipart_chunksize=8 * 1024 * 1024,
            use_threads=True,
        )
        client.upload_file(
            Filename=tmp_filename,
            Bucket=bucket_name,
            Key=object_key,
            Config=config,
            ExtraArgs={"ContentType": content_type},
        )
    finally:
        # Clean up
        if os.path.exists(tmp_filename):
            os.remove(tmp_filename)


# ---------------------------------------------------------------------------
# Cookie Management
# ---------------------------------------------------------------------------

COOKIE_R2_KEY = "config/cookies.txt"


def _parse_cookie_min_expiry(cookie_text: str) -> str | None:
    """
    Parse a Netscape-format cookie file and return the earliest expiry
    timestamp (ISO 8601) of security-critical YouTube cookies.

    Returns None if no valid expiry is found.
    """
    from datetime import datetime, timezone

    # YouTube auth cookies that matter for bot detection
    important_cookies = {"LOGIN_INFO", "SID", "HSID", "SSID", "APISID", "SAPISID", "__Secure-1PSID", "__Secure-3PSID"}

    min_ts = None
    for line in cookie_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        cookie_name = parts[5]
        try:
            expires_ts = int(parts[4])
        except (ValueError, IndexError):
            continue
        # 0 means session cookie — skip
        if expires_ts == 0:
            continue
        if cookie_name in important_cookies:
            if min_ts is None or expires_ts < min_ts:
                min_ts = expires_ts

    if min_ts is not None:
        return datetime.fromtimestamp(min_ts, tz=timezone.utc).isoformat()
    return None


def upload_cookies_to_r2(cookie_content: str, bucket_name: str, expires: str | None = None) -> dict:
    """
    Upload Netscape cookie text to R2 with expiry metadata.

    Args:
        cookie_content: Full Netscape-format cookie text.
        bucket_name: R2 bucket name.
        expires: Optional TTL string like "4h", "30m", "1d", or ISO datetime.
                 If not provided, auto-detects from cookie file content.

    Returns:
        Dict with upload status and expiry info.
    """
    from datetime import datetime, timedelta, timezone
    import re

    client = get_s3_client()
    now = datetime.now(timezone.utc)

    # Determine expires_at
    expires_at = None
    expiry_source = None

    if expires:
        # Parse human-friendly TTL like "4h", "30m", "1d"
        match = re.match(r"^(\d+)([mhd])$", expires.strip())
        if match:
            amount, unit = int(match.group(1)), match.group(2)
            delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
            expires_at = (now + delta).isoformat()
            expiry_source = "manual"
        else:
            # Try parsing as ISO datetime
            try:
                datetime.fromisoformat(expires)
                expires_at = expires
                expiry_source = "manual"
            except ValueError:
                pass

    if not expires_at:
        # Auto-detect from cookie content
        parsed = _parse_cookie_min_expiry(cookie_content)
        if parsed:
            expires_at = parsed
            expiry_source = "auto-detected"

    # Build metadata
    metadata = {"uploaded_at": now.isoformat()}
    if expires_at:
        metadata["expires_at"] = expires_at

    client.put_object(
        Bucket=bucket_name,
        Key=COOKIE_R2_KEY,
        Body=cookie_content.encode("utf-8"),
        ContentType="text/plain",
        Metadata=metadata,
    )

    return {
        "status": "uploaded",
        "key": COOKIE_R2_KEY,
        "uploaded_at": metadata["uploaded_at"],
        "expires_at": expires_at,
        "expiry_source": expiry_source,
    }


def get_cookies_from_r2(bucket_name: str) -> str | None:
    """
    Fetch cookie content from R2. Returns None if not found or expired.
    """
    from datetime import datetime, timezone

    client = get_s3_client()
    try:
        obj = client.get_object(Bucket=bucket_name, Key=COOKIE_R2_KEY)
    except Exception:
        return None

    # Check expiry
    metadata = obj.get("Metadata", {})
    expires_at = metadata.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) > expiry:
                # Cookie expired — don't use stale cookies
                return None
        except ValueError:
            pass

    return obj["Body"].read().decode("utf-8")


def get_cookies_status(bucket_name: str) -> dict | None:
    """
    Get cookie metadata (uploaded_at, expires_at, expired) without downloading content.
    Returns None if no cookies are stored.
    """
    from datetime import datetime, timezone

    client = get_s3_client()
    try:
        obj = client.head_object(Bucket=bucket_name, Key=COOKIE_R2_KEY)
    except Exception:
        return None

    metadata = obj.get("Metadata", {})
    expires_at = metadata.get("expires_at")
    expired = False
    if expires_at:
        try:
            expired = datetime.now(timezone.utc) > datetime.fromisoformat(expires_at)
        except ValueError:
            pass

    return {
        "key": COOKIE_R2_KEY,
        "uploaded_at": metadata.get("uploaded_at"),
        "expires_at": expires_at,
        "expired": expired,
        "size": obj.get("ContentLength"),
    }


def delete_cookies_from_r2(bucket_name: str) -> None:
    """Delete stored cookies from R2."""
    client = get_s3_client()
    client.delete_object(Bucket=bucket_name, Key=COOKIE_R2_KEY)
