# ADR-0007: Redis Streams behind a synchronous `MessageBus` port with a JSON envelope

Date: 2026-09-03

## Status

Accepted

## Context

The real-time lane is a chain of stages connected by topics `serac.waveforms`,
`serac.detections`, `serac.alerts`. The brief fixes Redis Streams for development and asks
for a port so NATS or Kafka can replace it later. The dev machine has no Redis.

## Decision

- Port `src/serac/ports/bus.py`: synchronous `MessageBus` with `publish`, `ensure_group`,
  `read` (consumer groups), `ack`, `pending`, `close`.
- Wire format: a JSON `Envelope` (`message_id, topic, schema_name, schema_version,
  producer, produced_at_utc, stream_time_utc, causation_id, replay_run_id, payload`), bytes
  base64-encoded, payload validated by schema name through a codec registry that rejects
  major-version mismatches.
- Adapters: `InMemoryBus` (round-trips through the codec on publish; deterministic
  single-thread `Pipeline.drain` for tests and replay) and `RedisStreamsBus`
  (XADD / XREADGROUP / XACK / XPENDING).
- `RedisStreamsBus` is unit-tested against `fakeredis` so the offline suite covers it. The
  live-server test carries the `redis` marker and is skipped without a reachable
  `SERAC_REDIS_URL`.

## Consequences

- Swapping the broker means one new adapter and no change to stages.
- Live Redis behaviour (blocking reads, pending-entry recovery) is **unverified** on the
  dev machine; `RELEASE_STATUS.md` records `tested-online: no` until `make smoke-online`
  runs against `infra/docker/compose.yaml`.
- Synchronous by design: stage code stays simple and replay stays deterministic.
