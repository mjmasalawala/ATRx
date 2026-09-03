"""
Same login-flow logic as the scalping strategy's kite_auth.py, duplicated
here so this screener works as a fully standalone project. Kite access
tokens expire daily; a saved token is reused until it's found to be invalid.
"""

import logging
from kiteconnect import KiteConnect, exceptions as kite_exceptions

from config import CONFIG

logger = logging.getLogger("atrx.auth")


def get_kite_session() -> KiteConnect:
    if not CONFIG.api_key or not CONFIG.api_secret:
        raise RuntimeError(
            "KITE_API_KEY / KITE_API_SECRET are not set. Export them as "
            "environment variables before running the screener."
        )

    kite = KiteConnect(api_key=CONFIG.api_key)

    token = _read_saved_token()
    if token:
        kite.set_access_token(token)
        if _token_is_valid(kite):
            logger.info("Reusing saved access token.")
            return kite
        logger.info("Saved access token is no longer valid; re-authenticating.")

    return _interactive_login(kite)


def _read_saved_token() -> str | None:
    try:
        return CONFIG.access_token_file.read_text().strip() or None
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Could not read saved access token: %s", e)
        return None


def _token_is_valid(kite: KiteConnect) -> bool:
    try:
        kite.profile()
        return True
    except kite_exceptions.TokenException:
        return False
    except Exception as e:
        logger.warning("Could not verify saved token (%s); assuming valid.", e)
        return True


def _interactive_login(kite: KiteConnect) -> KiteConnect:
    print(f"Login URL: {kite.login_url()}")
    request_token = input(
        "Log in via the URL above, then paste the 'request_token' from "
        "the redirect URL here: "
    ).strip()

    try:
        session = kite.generate_session(request_token, api_secret=CONFIG.api_secret)
    except kite_exceptions.KiteException as e:
        raise RuntimeError(f"Login failed: {e}") from e

    access_token = session["access_token"]
    kite.set_access_token(access_token)

    try:
        CONFIG.access_token_file.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.access_token_file.write_text(access_token)
    except OSError as e:
        logger.warning("Could not save access token to disk: %s", e)

    logger.info("Login successful.")
    return kite
