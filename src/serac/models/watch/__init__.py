"""M3 — slope watch: the kinematic anomaly layer.

serac's L0/L1 layer. It ranks slope units by destabilisation evidence from InSAR and optical
feature tracking and assigns each an ordinal watch tier (Quiet / Elevated / Watch).

**It never outputs a failure date and never outputs a calibrated failure probability.** With
one positive event in the archive no ROC is claimable and no probability is estimable; the
tier is an ordinal ranking whose thresholds were pre-registered before any backtest ran
(`reports/watch/PREREGISTRATION.md`). See `reports/MODEL_CARD_watch.md`.
"""

from __future__ import annotations

WATCH_VERSION = "0.1.0"
