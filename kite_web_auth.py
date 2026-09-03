"""
Kite login for the web deployment. Unlike kite_auth.py (interactive, paste
the request_token into a terminal prompt), this drives the same OAuth
exchange through HTTP redirects so a browser button click can trigger it.

The redirect URL a Kite Connect app sends the user back to after login is
configured once in the Kite developer console, not passed at request time --
set it there to https://<your-vercel-domain>/api/callback.
"""

from kiteconnect import KiteConnect, exceptions as kite_exceptions

from config import CONFIG


def build_login_url() -> str:
    if not CONFIG.api_key:
        raise RuntimeError("KITE_API_KEY is not set.")
    return KiteConnect(api_key=CONFIG.api_key).login_url()


def exchange_request_token(request_token: str) -> str:
    if not CONFIG.api_key or not CONFIG.api_secret:
        raise RuntimeError("KITE_API_KEY / KITE_API_SECRET are not set.")
    kite = KiteConnect(api_key=CONFIG.api_key)
    try:
        session = kite.generate_session(request_token, api_secret=CONFIG.api_secret)
    except kite_exceptions.KiteException as e:
        raise RuntimeError(f"Login failed: {e}") from e
    return session["access_token"]


def get_kite_session_from_token(access_token: str) -> KiteConnect:
    kite = KiteConnect(api_key=CONFIG.api_key)
    kite.set_access_token(access_token)
    return kite
