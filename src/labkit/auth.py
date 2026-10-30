"""Access checks shared by instrument services, using only the standard library."""

import hmac
import ipaddress
from urllib.parse import urlsplit

FORWARDED_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP")


def valid_token(value):
    """Accept printable ASCII without spaces at either end of the token."""
    return all(" " <= char <= "~" for char in value) and value == value.strip(" ")


def loopback_name(name, *, case_sensitive_localhost=False):
    """Check a loopback address or localhost name without resolving DNS.

    The robot startup check requires lowercase localhost. The stage accepts any case.
    """
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return (name if case_sensitive_localhost else name.lower()) == "localhost"


def valid_token_for_bind(token, bind, *, require_long_token=True, case_sensitive_localhost=False):
    """Require a browser token of at least 24 characters outside loopback.

    By default, a configured token must also be long on loopback, as the stage requires.
    The robot retains its existing short-token allowance with require_long_token=False.
    """
    value = token or ""
    if not valid_token(value):
        return False
    needs_long_token = (require_long_token and bool(value)) or not loopback_name(
        bind, case_sensitive_localhost=case_sensitive_localhost
    )
    return not needs_long_token or len(value) >= 24


def host_port(value, default_port):
    """Parse a Host header with the services' existing URL and default-port rules."""
    try:
        parts = urlsplit("//" + value.strip())
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.path or parts.query or parts.fragment or parts.username:
        return None
    return parts.hostname.lower(), port or default_port


def loopback_host(value):
    """Check that a Host header names a loopback interface."""
    parts = host_port(value, 80)
    return parts is not None and loopback_name(parts[0])


def has_forwarded_headers(headers):
    """Detect proxy headers, including headers with empty values."""
    return any(headers.get(name) is not None for name in FORWARDED_HEADERS)


def host_allowed(headers, token=None):
    """Without a token, require a loopback Host and no proxy headers."""
    return bool(token) or (
        not has_forwarded_headers(headers) and loopback_host(headers.get("Host", ""))
    )


def bearer_matches(authorization, token):
    """Compare a bearer header with the configured token without normalizing it."""
    return hmac.compare_digest(
        authorization.encode("latin-1", "replace"),
        f"Bearer {token}".encode("latin-1", "replace"),
    )


def same_origin(headers):
    """Refuse WebSocket requests from other origins. Programs may omit Origin.

    Headers must provide get_all (HTTPMessage) or getlist (Werkzeug) to retain duplicates.
    The first forwarded host can match the origin when the request comes through a proxy.
    """
    if headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
        return False
    get_all = getattr(headers, "get_all", None) or headers.getlist
    origins = get_all("Origin") or []
    if not origins:
        return True
    if len(origins) != 1:
        return False
    try:
        parts = urlsplit(origins[0].strip())
        port = parts.port
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.path:
        return False
    default_port = 443 if parts.scheme == "https" else 80
    origin = (parts.hostname.lower(), port or default_port)
    hosts = [headers.get("Host", ""), headers.get("X-Forwarded-Host", "").split(",")[0]]
    return any(host.strip() and host_port(host, default_port) == origin for host in hosts)
