"""Port for outbound alert delivery.

A `CAPMessage` that has been rendered and validated still has to reach somebody. `AlertSink`
is the seam: `serac.alerting` decides *what* to say, an adapter in
`serac.adapters.alerting` decides *where* it goes. Two adapters exist -- a file/log sink and
an HTTP POST sink -- and neither is wired to anything by default.

**Nothing is sent anywhere unless an operator names a destination.** The HTTP sink cannot be
constructed without an explicit endpoint, and `AlertSink.deliver` returns an `AlertDelivery`
record rather than raising, so a dispatch that failed is a fact in a report instead of a
stack trace that loses the message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.domain.cap import CAPMessage
from serac.errors import SeracError

ALERT_SINK_PORT_VERSION = "0.1.0"


class AlertSinkError(SeracError):
    """A sink could not be constructed or configured. Delivery failures are not errors."""


class AlertDelivery(BaseModel):
    """What one sink did with one message. `delivered=False` is a result, not an exception."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sink: str = Field(min_length=1)
    identifier: str = Field(min_length=1, description="The CAP message identifier")
    delivered: bool
    target: str | None = Field(
        default=None, description="Where it went: a path, a URL, or None when nothing was sent."
    )
    attempted_utc: AwareDatetime
    detail: str = Field(default="", description="Human-readable outcome, including any failure")
    signed: bool = Field(default=False, description="Whether the delivered XML carried a signature")


class AlertSink(ABC):
    """Somewhere a CAP message can be delivered."""

    name: str

    @abstractmethod
    def deliver(self, message: CAPMessage) -> AlertDelivery:
        """Attempt delivery and report what happened. Must not raise on a transport failure."""
