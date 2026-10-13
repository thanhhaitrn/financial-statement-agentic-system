"""Validate redirect targets before urllib sends the next HTTP request."""

from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener


def allowed_https_url(value: str, suffixes: tuple[str, ...]) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https" and parsed.port in (None, 443)
            and parsed.username is None and parsed.password is None
            and any(host == suffix.lstrip(".") or host.endswith("." + suffix.lstrip("."))
                    for suffix in suffixes)
        )
    except ValueError:
        return False


class AllowlistedRedirectHandler(HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, allowed, error_factory=ValueError):
        self.allowed = allowed
        self.error_factory = error_factory

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allowed(newurl):
            raise self.error_factory("Redirect target is outside the allowed HTTPS hosts")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def allowlisted_opener(allowed, error_factory=ValueError):
    return build_opener(AllowlistedRedirectHandler(allowed, error_factory)).open
