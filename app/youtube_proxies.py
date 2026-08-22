"""Validation and display helpers for the yt-dlp proxy pool.

Collectors receive one already-validated URL. Raw credentials never need to be rendered,
logged or stored in processing-run details, so masking stays here at the boundary shared by
the environment and the user-facing settings page.
"""

import re
from collections.abc import Iterable
from urllib.parse import unquote, urlsplit

SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
_SEPARATOR = re.compile(r"[\r\n,]+")


class InvalidYouTubeProxy(ValueError):
    """A proxy URL cannot safely be handed to yt-dlp."""


def validate_proxy_url(value: str) -> str:
    """Return a normalized yt-dlp proxy URL or raise without echoing the secret."""
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        raise InvalidYouTubeProxy("Proxy URL is empty or contains whitespace")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise InvalidYouTubeProxy("Proxy URL contains an invalid port or address") from exc

    scheme = parsed.scheme.casefold()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise InvalidYouTubeProxy("Proxy protocol is not supported")
    if not parsed.hostname or port is None:
        raise InvalidYouTubeProxy("Proxy URL must contain a host and port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise InvalidYouTubeProxy("Proxy URL cannot contain a path, query or fragment")

    authority = candidate.split("://", 1)[1].rstrip("/")
    return f"{scheme}://{authority}"


def parse_proxy_list(value: str | Iterable[str] | None) -> list[str]:
    """Parse a comma/newline env value or a stored JSON list, preserving order."""
    if value is None:
        return []
    raw_values = _SEPARATOR.split(value) if isinstance(value, str) else list(value)
    proxies: list[str] = []
    for raw in raw_values:
        if not str(raw).strip():
            continue
        proxy = validate_proxy_url(str(raw))
        if proxy not in proxies:
            proxies.append(proxy)
    return proxies


def mask_proxy_url(value: str) -> str:
    """Describe a proxy without disclosing credentials, server or port."""
    parsed = urlsplit(validate_proxy_url(value))
    credentials = "***:***@" if parsed.username is not None else ""
    return f"{parsed.scheme}://{credentials}***:***"


def redact_proxy_from_message(message: str, proxy: str | None) -> str:
    """Keep provider diagnostics useful without persisting the selected secret."""
    if not proxy:
        return message
    redacted = message.replace(proxy, mask_proxy_url(proxy))
    parsed = urlsplit(proxy)
    endpoint = f"{parsed.hostname}:{parsed.port}"
    redacted = redacted.replace(parsed.netloc, "***:***").replace(endpoint, "***:***")
    for secret in (parsed.username, parsed.password):
        decoded = unquote(secret or "")
        if len(decoded) >= 3:
            redacted = redacted.replace(decoded, "***")
    if parsed.hostname and len(parsed.hostname) >= 3:
        redacted = redacted.replace(parsed.hostname, "***")
    return redacted
