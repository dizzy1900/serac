# ADR-0011: MiniSEED bytes as the on-bus waveform payload

Date: 2026-09-03

## Status

Accepted

## Context

`SeismicTrace` chunks travel from the SeedLink ingestor (and from replay) to the detector
over the bus. Options were decoded float arrays or the native MiniSEED records.

## Decision

- The `SeismicTrace` payload carries `encoding: miniseed` with `data: bytes` (Steim2
  MiniSEED, 512-byte records) plus `data_sha256`, `sncl`, `start/end`, `sampling_rate_hz`,
  `npts`, `units=counts`, `quality`, `sequence` and a `TraceProvenance` block
  (`source ∈ {seedlink, fdsn, fixture, synthetic}`, `server`, `retrieved_at`, `licence`,
  `notes` required when synthetic). `float32le` is an allowed alternative encoding for
  synthetic bursts.
- Only `adapters/seismic/obspy_codec.py` encodes/decodes; stages call the codec through a
  port.
- SeedLink publishes one chunk per received record; replay slices archives into 5 s chunks
  in stream-time order.

## Consequences

- What arrives from the network is what goes on the bus; no lossy re-encoding, checksums
  match the archived fixture bytes.
- Consumers must decode MiniSEED; the codec adapter is the single place that does.
