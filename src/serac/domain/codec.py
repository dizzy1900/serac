"""Wire codec for bus envelopes.

Every message on the bus is a JSON `Envelope` whose `payload` is validated against the model
registered under `schema_name`. `decode` rejects unknown schema names and any envelope whose
`schema_version` major differs from the registered model's, so a producer and consumer built
from incompatible contracts fail loudly instead of silently mis-parsing bytes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from serac.domain.cap import CAP_CONTRACT_VERSION, CAPMessage
from serac.domain.detection import DETECTION_CONTRACT_VERSION, DetectionCandidate
from serac.domain.envelope import Envelope
from serac.domain.force_history import FORCE_HISTORY_CONTRACT_VERSION, ForceHistory
from serac.domain.seismic import SEISMIC_CONTRACT_VERSION, SeismicTrace
from serac.errors import SeracError


class CodecError(SeracError):
    """Envelope could not be encoded or decoded against the schema registry."""


@dataclass(frozen=True)
class SchemaSpec:
    """A registered payload contract."""

    name: str
    version: str
    model: type[BaseModel]

    @property
    def major(self) -> int:
        return int(self.version.split(".", 1)[0])


SCHEMA_REGISTRY: dict[str, SchemaSpec] = {
    spec.name: spec
    for spec in (
        SchemaSpec("seismic-trace", SEISMIC_CONTRACT_VERSION, SeismicTrace),
        SchemaSpec("detection-candidate", DETECTION_CONTRACT_VERSION, DetectionCandidate),
        SchemaSpec("force-history", FORCE_HISTORY_CONTRACT_VERSION, ForceHistory),
        SchemaSpec("cap-message", CAP_CONTRACT_VERSION, CAPMessage),
    )
}

_MODEL_TO_SPEC: dict[type[BaseModel], SchemaSpec] = {
    spec.model: spec for spec in SCHEMA_REGISTRY.values()
}


def spec_for(payload: BaseModel) -> SchemaSpec:
    """Registry entry for a payload instance, by exact model class."""
    try:
        return _MODEL_TO_SPEC[type(payload)]
    except KeyError as exc:
        raise CodecError(f"payload type {type(payload).__name__} is not registered") from exc


def wrap[PayloadT: BaseModel](
    payload: PayloadT,
    *,
    topic: str,
    producer: str,
    stream_time_utc: datetime,
    causation_id: str | None = None,
    replay_run_id: str | None = None,
    produced_at_utc: datetime | None = None,
) -> Envelope[PayloadT]:
    """Build an envelope whose schema name/version come from the registry."""
    spec = spec_for(payload)
    extra: dict[str, Any] = {}
    if produced_at_utc is not None:
        extra["produced_at_utc"] = produced_at_utc
    return Envelope[PayloadT](
        topic=topic,
        schema_name=spec.name,
        schema_version=spec.version,
        producer=producer,
        stream_time_utc=stream_time_utc,
        causation_id=causation_id,
        replay_run_id=replay_run_id,
        payload=payload,
        **extra,
    )


def _check_version(spec: SchemaSpec, schema_version: str) -> None:
    try:
        major = int(schema_version.split(".", 1)[0])
    except ValueError as exc:
        raise CodecError(f"malformed schema_version {schema_version!r}") from exc
    if major != spec.major:
        raise CodecError(
            f"schema {spec.name!r} major version mismatch: envelope says {schema_version}, "
            f"registry has {spec.version}"
        )


def encode(envelope: Envelope[Any]) -> bytes:
    """Serialise an envelope to UTF-8 JSON, checking it against the registry first."""
    spec = SCHEMA_REGISTRY.get(envelope.schema_name)
    if spec is None:
        raise CodecError(f"unknown schema name {envelope.schema_name!r}")
    if not isinstance(envelope.payload, spec.model):
        raise CodecError(
            f"payload is {type(envelope.payload).__name__}, "
            f"schema {spec.name!r} expects {spec.model.__name__}"
        )
    _check_version(spec, envelope.schema_version)
    return envelope.model_dump_json().encode("utf-8")


def decode(raw: bytes) -> Envelope[BaseModel]:
    """Parse UTF-8 JSON into a typed envelope; the payload model is chosen by `schema_name`.

    The bytes are parsed twice on purpose: once as plain JSON to read the schema header, then in
    pydantic JSON mode against `Envelope[<registered model>]` so bytes fields decode from base64
    (python-mode validation would treat a base64 string as UTF-8 bytes).
    """
    try:
        header = json.loads(raw)
    except ValueError as exc:
        raise CodecError(f"malformed envelope: {exc}") from exc
    if not isinstance(header, dict):
        raise CodecError("malformed envelope: top level is not an object")
    schema_name = header.get("schema_name")
    schema_version = header.get("schema_version")
    if not isinstance(schema_name, str) or not isinstance(schema_version, str):
        raise CodecError("malformed envelope: schema_name/schema_version missing")
    spec = SCHEMA_REGISTRY.get(schema_name)
    if spec is None:
        raise CodecError(f"unknown schema name {schema_name!r}")
    _check_version(spec, schema_version)
    envelope_cls = Envelope.__class_getitem__(spec.model)
    if not (isinstance(envelope_cls, type) and issubclass(envelope_cls, BaseModel)):
        raise CodecError("parametrising Envelope did not yield a model class")
    try:
        envelope = envelope_cls.model_validate_json(raw)
    except ValidationError as exc:
        raise CodecError(f"envelope does not satisfy {spec.name!r}: {exc}") from exc
    if not isinstance(envelope, Envelope):  # pragma: no cover - pydantic guarantees this
        raise CodecError("parametrised envelope did not produce an Envelope")
    return envelope
