# M2 force-history inversion — Chamoli rock and ice avalanche, 7 February 2021

- Target id: `chamoli-2021` (reproduction)
- Origin: 2021-02-07T04:51:18+00:00
- Nominal source: 30.3485, 79.7759
- Config hash: `4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988`
- Wall clock: 6.7 s
- Status: **failed**

## Station geometry

7 stations / 27 channels, azimuthal gap 180 deg, distance 6.43-11.43 deg, median pre-event SNR 0.70.

| channel | distance (deg) | azimuth (deg) | peak displacement (m) | SNR |
|---|---:|---:|---:|---:|
| `II.NIL.00.LH1` | 6.43 | 302 | 1.23e-05 | 0.51 |
| `II.NIL.00.LH2` | 6.43 | 302 | 8.72e-06 | 0.45 |
| `II.NIL.10.LH1` | 6.43 | 302 | 1.30e-05 | 0.46 |
| `II.NIL.10.LH2` | 6.43 | 302 | 7.59e-06 | 0.55 |
| `II.NIL.10.LHZ` | 6.43 | 302 | 5.86e-06 | 1.33 |
| `IC.LSA.00.LH1` | 9.86 | 91 | 3.06e-06 | 0.72 |
| `IC.LSA.00.LH2` | 9.86 | 91 | 2.08e-06 | 0.80 |
| `IC.LSA.00.LHZ` | 9.86 | 91 | 4.82e-06 | 0.40 |
| `IC.LSA.10.LH1` | 9.86 | 91 | 2.97e-06 | 0.70 |
| `IC.LSA.10.LH2` | 9.86 | 91 | 1.97e-06 | 0.72 |
| `IC.LSA.10.LHZ` | 9.86 | 91 | 4.78e-06 | 0.40 |
| `G.WUS.00.LHE` | 10.84 | 358 | 1.05e-05 | 0.49 |
| `G.WUS.00.LHN` | 10.84 | 358 | 1.82e-05 | 0.37 |
| `G.WUS.00.LHZ` | 10.84 | 358 | 7.66e-06 | 1.01 |
| `G.WUS.10.LHE` | 10.84 | 358 | 1.05e-05 | 0.51 |
| `G.WUS.10.LHN` | 10.84 | 358 | 1.89e-05 | 0.37 |
| `G.WUS.10.LHZ` | 10.84 | 358 | 7.91e-06 | 0.99 |
| `KC.ASAI..LHE` | 10.87 | 347 | 8.06e-06 | 0.58 |
| `KC.ASAI..LHN` | 10.87 | 347 | 2.34e-05 | 0.33 |
| `KC.ASAI..LHZ` | 10.87 | 347 | 6.13e-06 | 1.38 |
| `XR.BN08..LH1` | 11.21 | 120 | 9.39e-05 | 1.55 |
| `XR.BN08..LH2` | 11.21 | 120 | 6.41e-05 | 1.22 |
| `XR.BN08..LHZ` | 11.21 | 120 | 6.40e-05 | 2.42 |
| `XR.BA20..LHZ` | 11.23 | 123 | 7.41e-06 | 0.67 |
| `XR.BA19..LH1` | 11.43 | 121 | 7.03e-05 | 1.49 |
| `XR.BA19..LH2` | 11.43 | 121 | 3.15e-05 | 1.02 |
| `XR.BA19..LHZ` | 11.43 | 121 | 1.98e-07 | 1.45 |

## Refusal

REFUSED: the best-fitting trial location explains only 0.089 of the data variance, below the floor of 0.20; 7 stations / 27 channels, azimuthal gap 180 deg, distance 6.43-11.43 deg, median pre-event SNR 0.70. A least-squares inversion of records that do not contain the signal still returns a smooth force history with a clean envelope, and an amplitude set by noise rather than by the event, so serac reports nothing. serac does not publish a source location it cannot support. No location, no mass and no force history are reported for this event. XR.BA20..LH1: peak amplitude 2.062e-04 m is 24x the median; dropped as a glitch XR.BA20..LH2: peak amplitude 4.838e-04 m is 55x the median; dropped as a glitch

serac refuses rather than guesses. A source location published from a station set this sparse would be a number with no evidence behind it, and the contract makes that impossible to emit: `status="failed"` histories may not carry a location, a mass or any force samples.

## Disagreement

No public mass or force figure was retrieved for this event in-session, so there is nothing to disagree with. That absence is the finding: serac's interval stands alone and has not been cross-checked against anyone else's.

