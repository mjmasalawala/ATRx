"""
Best-effort WhatsApp notifications via CallMeBot's free personal-use API,
following the same thin-wrapper-over-one-API pattern as notify.py's Resend
usage. CallMeBot has no delivery SLA -- a failed/late send must not break
whatever triggered it.

Requires CALLMEBOT_APIKEY (obtained by messaging CallMeBot's WhatsApp
number once from the recipient phone -- see callmebot.com). ALERT_PHONE is
who gets messaged (defaults to the number this agent was built for).
"""

import logging
import os

import requests

logger = logging.getLogger("atrx.whatsapp_notify")

CALLMEBOT_APIKEY = os.getenv("CALLMEBOT_APIKEY", "")
ALERT_PHONE = os.getenv("ALERT_PHONE", "+919970852786")

_CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"

# CallMeBot silently truncates/drops overly long messages; keep well under
# WhatsApp's own ~4096 char limit.
_MAX_TEXT_LEN = 1500


def send_login_needed_whatsapp() -> bool:
    """Sent when the candidate-scan agent can't run because there's no
    valid Kite session. Never raises -- a failed notification must not
    break the caller."""
    return send_whatsapp(
        "ATRx: candidate scan skipped -- no valid Kite session. "
        "Log in at the ATRx site to resume alerts today."
    )


def send_whatsapp(text: str) -> bool:
    if not CALLMEBOT_APIKEY:
        logger.warning("CALLMEBOT_APIKEY not set; skipping WhatsApp notification")
        return False
    if len(text) > _MAX_TEXT_LEN:
        text = text[: _MAX_TEXT_LEN - 20].rstrip() + "\n... (truncated)"
    try:
        resp = requests.get(
            _CALLMEBOT_URL,
            params={"phone": ALERT_PHONE, "text": text, "apikey": CALLMEBOT_APIKEY},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning("Failed to send WhatsApp notification: %s", e)
        return False
