"""Bearer-token helpers for the agent gateway, using only the standard library."""

import hmac
import ipaddress


def valid_token(value):
    """Accept printable ASCII without spaces at either end of the token."""
    return all(" " <= char <= "~" for char in value) and value == value.strip(" ")


def loopback_name(name, *, case_sensitive_localhost=False):
    """Check a loopback address or localhost name without resolving DNS."""
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return (name if case_sensitive_localhost else name.lower()) == "localhost"


def bearer_matches(authorization, token):
    """Compare a bearer header with the configured token without normalizing it."""
    return hmac.compare_digest(
        authorization.encode("latin-1", "replace"),
        f"Bearer {token}".encode("latin-1", "replace"),
    )
