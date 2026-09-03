"""Bus envelope: the metadata wrapper around every payload on the message bus.

`stream_time_utc` is the time the message is *about* (for a waveform chunk, its start time;
for a detection, the detection time in the data's clock). `produced_at_utc` is wall-clock time
at the producer. During replay the two diverge deliberately: stream-time latencies are always
meaningful, wall-clock latencies only at speed 1.0. See `serac.domain.replay`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

ENVELOPE_CONTRACT_VERSION = "0.1.0"


class Envelope[PayloadT: BaseModel](BaseModel):
    """Typed message wrapper. The wire form is JSON (see `serac.domain.codec`)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, ser_json_bytes="base64", val_json_bytes="base64"
    )

    contract_version: str = ENVELOPE_CONTRACT_VERSION
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    topic: str = Field(min_length=1)
    schema_name: str = Field(min_length=1, description="Key into `codec.SCHEMA_REGISTRY`.")
    schema_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    producer: str = Field(min_length=1, description="Stage or process name that published this.")
    produced_at_utc: AwareDatetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    stream_time_utc: AwareDatetime
    causation_id: str | None = Field(
        default=None, description="message_id of the message this one was derived from."
    )
    replay_run_id: str | None = Field(
        default=None, description="Set on every message produced by `serac replay`."
    )
    payload: PayloadT


CONTRACTS: dict[str, type[BaseModel]] = {"envelope": Envelope}
