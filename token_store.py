"""
Holds today's Kite access token between the OAuth callback and the
screener run. Vercel functions are stateless/ephemeral, so this can't live
in a local variable or file -- it's stored in Upstash Redis over its REST
API. Both the raw "Upstash for Redis" marketplace integration
(UPSTASH_REDIS_REST_URL/TOKEN) and Vercel's own KV product, which is also
Upstash-backed but names its vars KV_REST_API_URL/TOKEN, are supported.

Requires one of those two env var pairs to be set.
"""

import os

import requests

UPSTASH_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL") or "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN") or ""

TOKEN_KEY = "atrx:kite_access_token"
# Kite access tokens expire around 6am IST the next day; 20h keeps a safety
# margin without a stale token surviving into the following session.
TOKEN_TTL_SECONDS = 20 * 60 * 60


def _command(*args) -> object:
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        raise RuntimeError(
            "No Redis config found (checked UPSTASH_REDIS_REST_URL/TOKEN and KV_REST_API_URL/TOKEN)."
        )
    resp = requests.post(
        UPSTASH_URL,
        headers={
            "Authorization": f"Bearer {UPSTASH_TOKEN}",
            "Content-Type": "application/json",
        },
        json=list(args),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("result")


def save_access_token(token: str) -> None:
    _command("SET", TOKEN_KEY, token, "EX", str(TOKEN_TTL_SECONDS))


def load_access_token() -> str | None:
    return _command("GET", TOKEN_KEY)
