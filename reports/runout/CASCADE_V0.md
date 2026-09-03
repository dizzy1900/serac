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
48 valid members. The index is evaluated at the median deposit; the breach and
surge at the 95th percentile, so those are an upper case rather than a central one.

## Candidate damming sites

| Chainage (km) | Channel (m) | Deposit p50 | Deposit p95 | Index | p(dam) | Breach peak (m3/s) |
|---|---|---|---|---|---|---|
| 1.6 | 822.6 | 2.1 | 5.3 | 0.00 | 0.08 | 77 |
| 3.9 | 284.7 | 44.1 | 44.1 | 0.15 | 0.11 | 11448 |
| 7.9 | 469.8 | 69.2 | 176.7 | 0.15 | 0.11 | 307365 |
| 10.1 | 68.8 | 61.4 | 151.7 | 0.89 | 0.43 | 32838 |
| 12.4 | 83.4 | 23.1 | 115.3 | 0.28 | 0.14 | 51911 |
| 15.4 | 161.8 | 0.0 | 31.6 | 0.20 | 0.12 | 5200 |
| 19.4 | 70.8 | 0.0 | 62.1 | 0.88 | 0.42 | 25779 |

## Secondary-surge arrivals from the three most upstream sites

Minutes after the breach begins, not after the detachment.

| Dam at (km) | Transect | Travel (min) | Celerity (m/s) | Peak (m3/s) |
|---|---|---|---|---|
| 1.6 | `rasuwagadhi-gyirong` | 23.4 | 10.8 | 77 |
| 1.6 | `syabrubesi` | 45.3 | 10.8 | 77 |
| 1.6 | `betrawati` | 93.3 | 10.8 | 77 |
| 1.6 | `galchhi` | 146.5 | 10.8 | 77 |
| 3.9 | `rasuwagadhi-gyirong` | 6.9 | 31.2 | 11448 |
| 3.9 | `syabrubesi` | 14.6 | 31.2 | 11448 |
| 3.9 | `betrawati` | 31.3 | 31.2 | 11448 |
| 3.9 | `galchhi` | 49.8 | 31.2 | 11448 |
| 7.9 | `rasuwagadhi-gyirong` | 2.4 | 62.4 | 307365 |
| 7.9 | `syabrubesi` | 6.2 | 62.4 | 307365 |
| 7.9 | `betrawati` | 14.5 | 62.4 | 307365 |
| 7.9 | `galchhi` | 23.8 | 62.4 | 307365 |

