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
