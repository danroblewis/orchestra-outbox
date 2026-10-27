"""JSON over HTTP for the senders: the token sent only to the configured API's own origin, and every
failure turned into a ``SendError`` with a code and never a body (a body may echo the draft's text).

A redirect to another origin is refused (urllib's default copies the Authorization header to
wherever a redirect points)."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from . import SendError


def origin(url: str) -> tuple[str, str]:
    u = urllib.parse.urlsplit(url)
    return u.scheme.lower(), u.netloc.lower()


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    def __init__(self, base: str):
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(urllib.parse.urljoin(req.full_url, newurl)) != origin(self.base):
            raise SendError("redirect_refused", permanent=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_after(headers) -> float | None:
    try:
        return max(float(headers.get("retry-after")), 0.0) if headers.get("retry-after") else None
    except (TypeError, ValueError):
        return None


class JSONClient:
    def __init__(self, base: str, headers: dict[str, str], timeout: float = 30.0):
        self.base = base.rstrip("/")
        self._headers = headers
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SameOrigin(self.base))

    def __repr__(self) -> str:                    # never the token
        return f"JSONClient({self.base!r})"

    def call(self, method: str, path: str, *, query: dict | None = None, body: dict | None = None) -> tuple[int, dict, object]:
        """``(status, headers, json)`` for a 2xx or 4xx answer; raises ``SendError`` for anything that
        leaves the outcome unknown (no answer, a 5xx, an answer that is not JSON)."""
        url = f"{self.base}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        if origin(url) != origin(self.base):
            raise SendError("origin_refused", permanent=True)
        data = json.dumps(body).encode() if body is not None else None
        headers = {**self._headers, **({"Content-Type": "application/json; charset=utf-8"} if data else {})}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                status, hdrs, raw = r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            status, hdrs = e.code, {k.lower(): v for k, v in (e.headers or {}).items()}
            try:
                raw = e.read()
            except (OSError, http.client.HTTPException):
                raw = b""
        except SendError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            raise SendError(f"network:{type(e).__name__}") from None
        if status >= 500:
            raise SendError(f"http_{status}", retry_after=_retry_after(hdrs))
        if status == 429:
            raise SendError("rate_limited", retry_after=_retry_after(hdrs) or 60.0)
        try:
            parsed = json.loads(raw or b"null")
        except ValueError:
            raise SendError(f"http_{status}_not_json") from None
        return status, hdrs, parsed
