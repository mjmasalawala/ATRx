"""
Persists each run's CSV to Vercel Blob storage for later analytics -- the
function's own filesystem doesn't survive between invocations, so this is
the only place output can actually accumulate across runs. Not checked into
git (see .gitignore for the local CLI equivalent, output/).

Requires BLOB_READ_WRITE_TOKEN (set automatically when you attach a Vercel
Blob store to the project). Talks to Vercel Blob's plain HTTP API directly
since there's no official Python SDK.
"""

import os

import requests

BLOB_TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN", "")
BLOB_BASE_URL = "https://blob.vercel-storage.com"


def upload_csv(pathname: str, content: bytes) -> str:
    if not BLOB_TOKEN:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set.")
    resp = requests.put(
        f"{BLOB_BASE_URL}/{pathname}",
        headers={
            "Authorization": f"Bearer {BLOB_TOKEN}",
            "x-api-version": "7",
            "x-content-type": "text/csv",
            "x-add-random-suffix": "0",
        },
        data=content,
        timeout=20,
    )
    if not resp.ok:
        # Vercel Blob's REST API isn't officially documented for non-JS
        # callers -- surface the real response body on failure rather than
        # a bare status code, since the exact required headers/shape have
        # had to be reverse-engineered.
        raise RuntimeError(f"Blob upload failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json().get("url")
