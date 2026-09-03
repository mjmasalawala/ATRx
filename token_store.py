"""
Holds today's Kite access token between the OAuth callback and the
screener run. Vercel functions are stateless/ephemeral, so this can't live
in a local variable or file -- it's stored in Upstash Redis (Vercel's
managed KV integration is Upstash-backed and exposes the same REST API).

Requires UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN env vars.
"""

import os

import requests

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

TOKEN_KEY = "atrx:kite_access_token"
# Kite access tokens expire around 6am IST the next day; 20h keeps a safety
# margin without a stale token surviving into the following session.
TOKEN_TTL_SECONDS = 20 * 60 * 60


def _command(*args) -> object:
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        raise RuntimeError("UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN are not set.")
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
