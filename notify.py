"""
Best-effort email notifications via Resend's HTTP API, following the same
thin-wrapper-over-one-API pattern as blob_store.py's Vercel Blob usage.

Requires RESEND_API_KEY. RESEND_FROM_EMAIL must be a sender address/domain
verified in Resend; NOTIFY_EMAIL is who gets notified (defaults to the
user's own address).
"""

import logging
import os

import requests

logger = logging.getLogger("atrx.notify")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "ATRx <onboarding@resend.dev>")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "mjmasalawala@gmail.com")

_RESEND_URL = "https://api.resend.com/emails"


def send_login_needed_email() -> bool:
    """Sent when the daily cost-basis sync can't run because there's no
    valid Kite session. Never raises -- a failed notification must not
    break the sync itself."""
    return _send(
        subject="ATRx: Cost Basis sync needs you to log in",
        text=(
            "Today's automatic cost-basis sync couldn't run because there's no "
            "valid Kite session saved.\n\n"
            "Log in at the ATRx site, then hit \"Sync now\" on the Cost Basis "
            "page to pull in today's trades."
        ),
    )


def _send(subject: str, text: str) -> bool:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set; skipping notification email (%s)", subject)
        return False
    try:
        resp = requests.post(
            _RESEND_URL,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json={
                "from": RESEND_FROM_EMAIL,
                "to": [NOTIFY_EMAIL],
                "subject": subject,
                "text": text,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Failed to send notification email (%s): %s", subject, e)
        return False
