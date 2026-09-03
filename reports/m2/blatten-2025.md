# M2 force-history inversion — Blatten / Birch Glacier collapse, 28 May 2025

- Target id: `blatten-2025` (new event)
- Origin: 2025-05-28T13:24:26.162933+00:00
- Nominal source: 46.4042, 7.8362
- Config hash: `4762e79e5879b7b2559a0f7d476dddd45fc2dd869858f7a5e023535ff1a0d988`
- Wall clock: 14.8 s
- Status: **failed**

## Station geometry

9 stations / 27 channels, azimuthal gap 78 deg, distance 0.97-14.64 deg, median pre-event SNR 1.82.

| channel | distance (deg) | azimuth (deg) | peak displacement (m) | SNR |
|---|---:|---:|---:|---:|
| `CH.MUGIO..LHE` | 0.97 | 120 | 5.41e-06 | 0.84 |
| `CH.MUGIO..LHN` | 0.97 | 120 | 2.37e-05 | 1.73 |
| `CH.MUGIO..LHZ` | 0.97 | 120 | 1.42e-06 | 4.20 |
| `MN.TUE..LHE` | 1.05 | 86 | 1.31e-06 | 1.21 |
| `MN.TUE..LHN` | 1.05 | 86 | 1.19e-06 | 1.28 |
| `MN.TUE..LHZ` | 1.05 | 86 | 1.28e-06 | 8.35 |
| `CH.BRANT..LHE` | 1.08 | 300 | 1.95e-05 | 0.96 |
| `CH.BRANT..LHN` | 1.08 | 300 | 1.14e-05 | 1.26 |
| `CH.BRANT..LHZ` | 1.08 | 300 | 5.76e-06 | 2.19 |
| `GU.RSP..LHE` | 1.32 | 198 | 7.24e-06 | 1.60 |
| `GU.RSP..LHN` | 1.32 | 198 | 1.52e-05 | 2.89 |
| `GU.RSP..LHZ` | 1.32 | 198 | 1.16e-05 | 4.46 |
| `FR.ILLF.00.LH1` | 1.34 | 343 | 4.60e-06 | 1.12 |
| `FR.ILLF.00.LH2` | 1.34 | 343 | 6.60e-06 | 3.14 |
| `FR.ILLF.00.LHZ` | 1.34 | 343 | 1.12e-06 | 2.64 |
| `FR.RIVEL.00.LH1` | 1.37 | 286 | 1.85e-06 | 0.37 |
| `FR.RIVEL.00.LH2` | 1.37 | 286 | 1.19e-06 | 0.91 |
| `FR.RIVEL.00.LHZ` | 1.37 | 286 | 1.04e-06 | 2.91 |
| `CH.SLE..LHE` | 1.43 | 18 | 3.08e-06 | 2.23 |
| `CH.SLE..LHN` | 1.43 | 18 | 1.28e-05 | 2.75 |
| `CH.SLE..LHZ` | 1.43 | 18 | 8.91e-07 | 4.15 |
| `OE.CONA.BH.LH1` | 5.68 | 72 | 5.26e-07 | 1.55 |
| `OE.CONA.BH.LH2` | 5.68 | 72 | 7.17e-07 | 1.43 |
| `OE.CONA.BH.LHZ` | 5.68 | 72 | 4.36e-07 | 6.56 |
| `PM.PVAQ..LH1` | 14.64 | 238 | 1.70e-06 | 1.82 |
| `PM.PVAQ..LH2` | 14.64 | 238 | 2.49e-06 | 1.32 |
| `PM.PVAQ..LHZ` | 14.64 | 238 | 1.11e-07 | 2.32 |

## Refusal

REFUSED: the best-fitting trial location explains only 0.191 of the data variance, below the floor of 0.20; 9 stations / 27 channels, azimuthal gap 78 deg, distance 0.97-14.64 deg, median pre-event SNR 1.82. A least-squares inversion of records that do not contain the signal still returns a smooth force history with a clean envelope, and an amplitude set by noise rather than by the event, so serac reports nothing. serac does not publish a source location it cannot support. No location, no mass and no force history are reported for this event.

serac refuses rather than guesses. A source location published from a station set this sparse would be a number with no evidence behind it, and the contract makes that impossible to emit: `status="failed"` histories may not carry a location, a mass or any force samples.

## Disagreement

- EGU 2026 abstract egu26-3801: 'buried the village of Blatten under 9 million m3 of ice and rock'.
- EGU 2026 abstract egu26-6599: 'a volume of approximately 10 million cubic meters'.
- Both are conference abstracts, which the serac event library does not treat as qualifying for a best value.

serac produced no estimate for this event -- see the refusal above -- so there is no number to compare. A published figure existing does not make serac's silence wrong, and serac's silence does not make the published figure wrong.

