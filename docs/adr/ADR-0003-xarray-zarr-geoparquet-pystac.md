# ADR-0003: xarray + Zarr v3 feature cubes, GeoParquet vectors, pystac catalogue

Date: 2026-09-03

## Status

Accepted

## Context

The batch lane needs a per-AOI, multi-layer, time-indexed raster store on a fixed 30 m grid
with per-layer provenance, plus vector storage for events, AOIs, transects and assets, plus
a discoverable catalogue. Recon found that xarray 2026.7 requires `zarr>=3.0` for its io
extra; the locked pin is zarr 3.3.

## Decision

- Feature cubes are `xarray.Dataset`s written with `zarr_format=3` (unconsolidated,
  chunks `1 x 512 x 512`, zstd) to `data/features/<aoi>/cube.zarr`. Coordinates `x, y, time,
  spatial_ref`. Per-layer attrs (`source, product_ids, manifest_entry_ids, retrieved_at,
  provenance, status, licence, units, processing, native_resolution_m`) and a global
  `contains_synthetic` flag are mandatory and checked by `make validate-cube`.
- A Zarr roundtrip test runs in CI. If the xarray/zarr-v3 pairing fails, the fallback is a
  single constant switching to v2 and an amendment to this ADR — not a silent change.
- Vectors are GeoParquet via geopandas 1.1 / pyarrow. The event library's canonical form is
  one reviewed JSON per record; `data/events/events.parquet` is a derived index.
- Catalogues are pystac 1.15: Catalog → Collection per AOI (`serac-cube-<aoi>`) → Item per
  time slice. STAC JSON schemas are vendored under `tests/fixtures/stac_schemas/` so
  validation runs offline.

## Consequences

- Missing layers are stored as all-NaN with `status: not_fetched` and a `<layer>_valid(time)`
  flag, so "no acquisition" is distinguishable from "acquired, NaN".
- Zarr v3 stores are not readable by zarr v2 clients; downstream consumers use the STAC
  catalogue and the contracts, not the store format.
