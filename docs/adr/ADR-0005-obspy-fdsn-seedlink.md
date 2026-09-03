# ADR-0005: ObsPy for FDSN and SeedLink; seisbench deferred to Prompt 2

Date: 2026-09-03

## Status

Accepted

## Context

The real-time lane needs archived waveforms (FDSN dataselect/station) for replay and
fixtures, and a live SeedLink client. Machine-learning pickers and discriminators are
Prompt 2 work.

## Decision

- `obspy` 1.5.1 is the only seismology dependency in Prompt 1: FDSN clients, SeedLink
  (`EasySeedLinkClient`), and MiniSEED encode/decode.
- Only `src/serac/adapters/seismic/obspy_codec.py` imports obspy for codec work; the domain
  never does. Adapters (`fdsn.py`, `seedlink.py`) own the client objects.
- `seisbench` is reserved for Prompt 2 and is not a dependency now.

## Consequences

- The Prompt 1 detector is a placeholder energy-ratio stub and says so in its docstring.
- SeedLink hostnames are configuration (`SERAC_SEEDLINK_SERVER`, default
  `geofon.gfz.de:18000`) flagged "verify with smoke-online", not asserted facts.
