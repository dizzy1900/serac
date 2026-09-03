# Cascade rules v0 — damming, breach, secondary surge

> NOT r.avaflow: flow depths, velocities and arrival times come from serac-swe-voellmy v0.2.0, a single-phase depth-averaged Voellmy-Salm solver implemented in this repository. r.avaflow could not be obtained (see infra/docker/ravaflow/README.md); cross-validation against r.avaflow is outstanding.

**Read this before using any number below.** 30 m DEM: the Bhote Koshi gorge is under 60 m wide in places, so it spans fewer than two cells. Superelevation, run-up on valley walls and channel blocking are unresolved; damming numbers derived from deposit depth against channel geometry are order-of-magnitude indicators, not engineering estimates.

These are a dimensionless index and a set of parametric relations, not an engineering estimate
of a landslide dam. In particular:

* the damming index is deposit depth over channel depth, **both measured on the same 30 m DEM**,
  so both sides of the ratio carry the same resolution error;
* the "probability" column is that index put through a stated logistic with a midpoint at index
  1 and a scale of 0.4. It is **not** estimated from data. No inventory of landslide dams exists
  for this corridor, and one event is not a sample;
* the breach hydrograph is a triangular wave whose area is the impounded volume and whose peak
  follows a published-form regression. It is not routed, has no sediment and no progressive
  erosion;
* the secondary-surge arrival translates that peak downstream at a constant celerity. It is not
  a solved flood routing, and it ignores attenuation, so it **over-states** the surge downstream.

Deposit depths are the ensemble median and 95th percentile per chainage bin over
230 valid members. The index is evaluated at the median deposit; the breach and
surge at the 95th percentile, so those are an upper case rather than a central one.

## Candidate damming sites

| Chainage (km) | Channel (m) | Deposit p50 | Deposit p95 | Index | p(dam) | Breach peak (m3/s) |
|---|---|---|---|---|---|---|
| 1.6 | 822.6 | 2.3 | 6.0 | 0.00 | 0.08 | 103 |
| 3.9 | 284.7 | 44.1 | 61.2 | 0.15 | 0.11 | 24881 |
| 7.9 | 469.8 | 43.5 | 187.1 | 0.09 | 0.09 | 352244 |
| 10.1 | 68.8 | 51.3 | 133.1 | 0.75 | 0.35 | 32838 |
| 12.4 | 83.4 | 20.7 | 98.6 | 0.25 | 0.13 | 51911 |
| 15.4 | 161.8 | 0.0 | 37.1 | 0.23 | 0.13 | 7588 |
| 19.4 | 70.8 | 0.0 | 71.3 | 1.01 | 0.50 | 35171 |

## Secondary-surge arrivals from the three most upstream sites

Minutes after the breach begins, not after the detachment.

| Dam at (km) | Transect | Travel (min) | Celerity (m/s) | Peak (m3/s) |
|---|---|---|---|---|
| 1.6 | `rasuwagadhi-gyirong` | 22.0 | 11.5 | 103 |
| 1.6 | `syabrubesi` | 42.6 | 11.5 | 103 |
| 1.6 | `betrawati` | 87.8 | 11.5 | 103 |
| 1.6 | `galchhi` | 137.8 | 11.5 | 103 |
| 3.9 | `rasuwagadhi-gyirong` | 5.9 | 36.7 | 24881 |
| 3.9 | `syabrubesi` | 12.4 | 36.7 | 24881 |
| 3.9 | `betrawati` | 26.5 | 36.7 | 24881 |
| 3.9 | `galchhi` | 42.2 | 36.7 | 24881 |
| 7.9 | `rasuwagadhi-gyirong` | 2.3 | 64.3 | 352244 |
| 7.9 | `syabrubesi` | 6.0 | 64.3 | 352244 |
| 7.9 | `betrawati` | 14.1 | 64.3 | 352244 |
| 7.9 | `galchhi` | 23.1 | 64.3 | 352244 |

