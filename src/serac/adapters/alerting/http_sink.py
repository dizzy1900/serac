"""POST a CAP message to an operator-supplied endpoint. Off unless somebody turns it on.

Three interlocks, because an alerting system that can send by accident is worse than one that
cannot send at all:

1. **No default endpoint.** `HttpAlertSink(endpoint=...)` is required and must be an absolute
   `http://` or `https://` URL. There is no fallback, no environment default read at import,
   and nothing in serac constructs one for you.
2. **`enabled` defaults to False.** A constructed-but-not-enabled sink returns
   `delivered=False` with `detail` saying it was not enabled. Enabling is a second, explicit
   act (`--send` on the CLI), so "I built the sink" and "I sent the message" cannot be the
   same mistake.
3. **Tests cannot reach the network.** `requests` is imported inside `deliver`, and the
   offline suite runs under pytest-socket, so a test that enabled this sink by accident fails
   mechanically rather than posting somewhere.

Transport failures are returned, never raised: a POST that timed out is a fact the replay
report should carry, not an exception that loses the message.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from serac.alerting.signing import is_signed
from serac.domain.cap import CAPMessage
from serac.ports.alert_sink import AlertDelivery, AlertSink, AlertSinkError

CAP_CONTENT_TYPE = "application/cap+xml"
DEFAULT_TIMEOUT_S = 10.0
NOT_ENABLED_DETAIL = (
    "HTTP sink constructed but not enabled; nothing was sent. Pass enabled=True (the CLI's "
    "--send) to transmit."
)


class HttpAlertSink(AlertSink):
    """POST the rendered CAP XML to one endpoint, only when explicitly enabled."""

    name = "http"

    def __init__(
        self,
        endpoint: str,
        *,
        enabled: bool = False,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        headers: dict[str, str] | None = None,
        session: Any | None = None,
    ) -> None:
        if not endpoint or not endpoint.startswith(("http://", "https://")):
            raise AlertSinkError(
                f"endpoint {endpoint!r} is not an absolute http(s) URL; the HTTP alert sink has "
                "no default destination and will not guess one"
            )
        self.endpoint = endpoint
        self.enabled = enabled
        self.timeout_s = timeout_s
        self.headers = {"Content-Type": CAP_CONTENT_TYPE, **(headers or {})}
        self.session = session
        self.posted = 0

    def deliver(self, message: CAPMessage) -> AlertDelivery:
        attempted = datetime.now(tz=UTC)
        signed = is_signed(message.xml) if message.xml else False
        if not message.xml:
            return AlertDelivery(
                sink=self.name,
                identifier=message.identifier,
                delivered=False,
                target=self.endpoint,
                attempted_utc=attempted,
                detail="message carries no rendered XML; render and validate it first",
            )
        if not self.enabled:
            return AlertDelivery(
                sink=self.name,
                identifier=message.identifier,
                delivered=False,
                target=self.endpoint,
                attempted_utc=attempted,
                detail=NOT_ENABLED_DETAIL,
                signed=signed,
            )
        poster = self.session
        if poster is None:
            import requests  # imported here so no test can reach the network by importing us

            poster = requests
        try:
            response = poster.post(
                self.endpoint,
                data=message.xml.encode("utf-8"),
                headers=self.headers,
                timeout=self.timeout_s,
            )
        # Broad on purpose: every transport failure is a result to report, not a crash.
        except Exception as exc:
            return AlertDelivery(
                sink=self.name,
                identifier=message.identifier,
                delivered=False,
                target=self.endpoint,
                attempted_utc=attempted,
                detail=f"{type(exc).__name__}: {exc}",
                signed=signed,
            )
        status = int(getattr(response, "status_code", 0))
        ok = 200 <= status < 300
        if ok:
            self.posted += 1
        return AlertDelivery(
            sink=self.name,
            identifier=message.identifier,
            delivered=ok,
            target=self.endpoint,
            attempted_utc=attempted,
            detail=f"HTTP {status}",
            signed=signed,
        )
