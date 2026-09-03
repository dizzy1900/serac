# ADR-0012: CAP 1.2 via lxml with vendored XSDs

Date: 2026-09-03

## Status

Accepted

## Context

The lane's public output is a Common Alerting Protocol v1.2 message. `make validate-stream`
must validate it offline. The CAP 1.2 XSD imports the XML-Signature schema.

## Decision

- CAP messages are rendered with `lxml` from the `CAPMessage` contract and validated
  against vendored `CAP-v1.2.xsd` + `xmldsig-core-schema.xsd` through a local resolver;
  the vendored files carry manifest entries (`source: vendored_schema`) with checksums.
  If the resolver cannot bind the import, a clearly named no-dsig copy is used and
  ledgered.
- The Prompt 1 `cap_stub` emits `status=Test`, `scope=Private`, sender
  `serac-stub@serac.invalid`, urgency/severity/certainty `Unknown`, and **omits `area`**
  because the stub has no location — no fabricated footprint.

## Consequences

- `validate-stream` proves schema validity of a Test message, nothing more.
- Real `Actual` alerts with an `area` require the Prompt 2 inversion and cascade outputs and
  a separate decision about senders and scope.
