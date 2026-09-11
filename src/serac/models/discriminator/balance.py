"""Equalising realised receiver counts between a positive and its matched negatives (Gap 17).

`catalog.py` already matches negatives to positives on the station *set*: a negative is windowed
at exactly the stations its positive was windowed at. That is the right rule and it is not the
whole story, because a station that is *requested* is not always *realised* — the instrument was
down, the channel was gapped, the trace failed a quality screen. What survives is a systematic
asymmetry, because a mass movement large enough to be catalogued is more likely to have been
recorded well:

    mass_movement   mean 8.38 receivers
    tectonic        mean 7.37 receivers        (+1.02 on a per-pair comparison)

No feature counts receivers. `valid_channel_fraction` was removed before the test set was scored
and `FORBIDDEN_FEATURE_TOKENS` keeps identity out of the names. It leaks anyway, because the
cross-receiver aggregates (`*_mad`, `*_p90`, `lp_envelope_coherence`) are computed over however
many traces contributed, so their sampling behaviour carries the count. Measured on the committed
index, **`n_stations` alone separates the classes at ROC-AUC 0.608** — a classifier given nothing
but "how many stations recorded this" beats chance, and all ten leakage assertions pass.

**The rule.** For each event group, take ``k`` = the smallest realised receiver count over the
positive and every window matched to it, and keep the first ``k`` occupied slots of each. Slots
are in the order the stations were requested, which is shared across the group, so truncating
preserves the identity matching the design already achieves: agreement between a positive's kept
stations and a member's is 83.1 %, against 83.5 % for the untruncated sets.

**What it costs and what it buys**, both measured on the committed index rather than asserted:

| | before | after |
|---|---|---|
| `n_stations`-alone ROC-AUC | 0.608 | **0.500** |
| receivers on a positive | 8.38 | 5.72 |
| groups lost entirely | — | 0 |

The residual is zero by construction within a group, and the *marginal* balance has to be checked
separately — a group's ``k`` could have correlated with how many negatives it has, and it does
not (r = -0.001), so the marginal AUC is 0.5001 rather than merely the conditional one.

**Two alternatives, both measured and both worse.** Intersecting the realised station *sets*
group-wise is stronger and costs 8.38 -> 4.60 receivers and four groups entirely. Doing it
pairwise costs only 8.38 -> 7.02 but gives a positive a different mask for every negative it is
compared against, which a single row in the store cannot carry.

**This does not retrain anything.** The store's samples are not in the repository, so no model has
been fitted under this rule and the effect on M1's reported skill is unknown. That is the open
question Gap 17 names, and closing the leak is the precondition for asking it, not the answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from serac.models.discriminator.dataset import DatasetIndex, WindowRecord

BALANCE_VERSION = "0.1.0"
POSITIVE_LABEL = "mass_movement"


@dataclass(frozen=True, slots=True)
class GroupQuota:
    """One event group's receiver budget and the members it binds."""

    positive_id: str
    quota: int
    member_ids: tuple[str, ...]
    realised: tuple[int, ...]
    """Realised receiver count per member, in ``member_ids`` order, before truncation."""

    @property
    def receivers_dropped(self) -> int:
        return sum(n - self.quota for n in self.realised)


@dataclass(frozen=True, slots=True)
class BalanceReport:
    """What the rule did, in numbers a model card can quote."""

    version: str
    n_groups: int
    n_windows_bound: int
    n_groups_emptied: int
    auc_before: float
    auc_after: float
    mean_receivers_before: dict[str, float]
    mean_receivers_after: dict[str, float]
    receivers_dropped: int
    mean_station_identity_agreement: float
    orphan_ids: tuple[str, ...] = ()
    """Windows matched to a positive that is not in the index. They cannot be balanced against
    anything, they keep their realised count, and they are why `auc_after` is not exactly 0.5."""
    auc_after_excluding_orphans: float = float("nan")

    def render(self) -> str:
        was = self.mean_receivers_before.get(POSITIVE_LABEL, 0.0)
        now = self.mean_receivers_after.get(POSITIVE_LABEL, 0.0)
        return (
            f"n_stations-alone ROC-AUC {self.auc_before:.4f} -> {self.auc_after:.4f} over "
            f"{self.n_groups} group(s); positives {was:.2f} -> {now:.2f} receivers; "
            f"{self.receivers_dropped} receiver-window(s) dropped; "
            f"{self.n_groups_emptied} group(s) emptied"
            + (
                f"; {len(self.orphan_ids)} orphaned window(s) left unbalanced "
                f"(AUC {self.auc_after_excluding_orphans:.4f} without them)"
                if self.orphan_ids
                else ""
            )
        )


def _members(index: DatasetIndex) -> tuple[dict[str, WindowRecord], dict[str, list[WindowRecord]]]:
    positives = {w.entry_id: w for w in index.windows if w.class_label == POSITIVE_LABEL}
    bound: dict[str, list[WindowRecord]] = {pid: [] for pid in positives}
    for w in index.windows:
        if w.class_label == POSITIVE_LABEL:
            continue
        if w.matched_positive_id in bound:
            bound[w.matched_positive_id].append(w)
    return positives, bound


def orphaned_windows(index: DatasetIndex) -> tuple[str, ...]:
    """Windows whose ``matched_positive_id`` names a positive this index does not contain.

    `CatalogEntry` validates that a negative *carries* a matched positive id; nothing checked
    that the positive is actually there. On the committed index 34 windows -- 33 tectonic and one
    noise -- inherit a split group from a parent that is absent, and their mean realised receiver
    count is 3.56 against 7.37 for tectonics generally. They cannot be balanced against anything,
    so they are named here rather than quietly carried: a trainer that wants the leak closed has
    to drop them, and one that keeps them should know the residual it is keeping.
    """
    positives = {w.entry_id for w in index.windows if w.class_label == POSITIVE_LABEL}
    return tuple(
        w.entry_id
        for w in index.windows
        if w.class_label != POSITIVE_LABEL and w.matched_positive_id not in positives
    )


def group_quotas(index: DatasetIndex) -> dict[str, GroupQuota]:
    """The receiver budget for every group, keyed by the positive's entry id.

    A window matched to a positive that is not in this index is left alone: it cannot be balanced
    against something absent, and silently dropping it would change the split composition.
    """
    positives, bound = _members(index)
    quotas: dict[str, GroupQuota] = {}
    for pid, positive in positives.items():
        members = [positive, *bound[pid]]
        realised = tuple(w.n_stations for w in members)
        quotas[pid] = GroupQuota(
            positive_id=pid,
            quota=min(realised),
            member_ids=tuple(w.entry_id for w in members),
            realised=realised,
        )
    return quotas


def station_masks(index: DatasetIndex) -> dict[str, tuple[int, ...]]:
    """Slot indices to keep, per window entry id.

    A window in no group keeps every slot it realised: balancing is a statement about a
    comparison, and a window with nothing to be compared against has none to make.
    """
    quotas = group_quotas(index)
    by_positive = {w.entry_id: w for w in index.windows if w.class_label == POSITIVE_LABEL}
    masks: dict[str, tuple[int, ...]] = {}
    for w in index.windows:
        pid = w.entry_id if w.entry_id in by_positive else w.matched_positive_id
        quota = quotas.get(pid or "")
        keep = w.n_stations if quota is None else min(quota.quota, w.n_stations)
        masks[w.entry_id] = tuple(range(keep))
    return masks


def n_stations_auc(
    index: DatasetIndex,
    *,
    negative_label: str = "tectonic",
    masks: dict[str, tuple[int, ...]] | None = None,
) -> float:
    """ROC-AUC of "how many stations recorded this" as a one-feature classifier.

    The leak metric. 0.5 is a receiver count that says nothing about the class; the committed
    index scores 0.608 unbalanced. Ties are given mid-ranks, which matters here because receiver
    counts are small integers and most of the mass sits on a handful of values.
    """

    def count(w: WindowRecord) -> int:
        return w.n_stations if masks is None else len(masks.get(w.entry_id, ()))

    positives = [count(w) for w in index.windows if w.class_label == POSITIVE_LABEL]
    negatives = [count(w) for w in index.windows if w.class_label == negative_label]
    return roc_auc(positives, negatives)


def roc_auc(positives: list[int], negatives: list[int]) -> float:
    """Mann-Whitney AUC with mid-ranks for ties. ``nan`` when either class is empty."""
    if not positives or not negatives:
        return float("nan")
    labelled = sorted([(v, 1) for v in positives] + [(v, 0) for v in negatives], key=lambda t: t[0])
    ranks = [0.0] * len(labelled)
    i = 0
    while i < len(labelled):
        j = i
        while j < len(labelled) and labelled[j][0] == labelled[i][0]:
            j += 1
        mid = (i + j + 1) / 2.0
        for k in range(i, j):
            ranks[k] = mid
        i = j
    rank_sum = sum(ranks[k] for k, (_, label) in enumerate(labelled) if label == 1)
    n_pos, n_neg = len(positives), len(negatives)
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def balance_report(index: DatasetIndex, *, negative_label: str = "tectonic") -> BalanceReport:
    """Apply the rule on paper and report what it would cost and buy."""
    quotas = group_quotas(index)
    masks = station_masks(index)
    positives, bound = _members(index)

    before: dict[str, list[int]] = {}
    after: dict[str, list[int]] = {}
    for w in index.windows:
        before.setdefault(w.class_label, []).append(w.n_stations)
        after.setdefault(w.class_label, []).append(len(masks[w.entry_id]))

    agreements: list[float] = []
    for pid, positive in positives.items():
        quota = quotas[pid].quota
        if quota == 0:
            continue
        kept = set(positive.station_keys[:quota])
        for member in bound[pid]:
            member_kept = set(member.station_keys[:quota])
            agreements.append(len(kept & member_kept) / quota)

    orphans = orphaned_windows(index)
    without_orphans = index.model_copy(
        update={"windows": [w for w in index.windows if w.entry_id not in set(orphans)]}
    )
    return BalanceReport(
        version=BALANCE_VERSION,
        n_groups=len(quotas),
        n_windows_bound=sum(len(q.member_ids) for q in quotas.values()),
        n_groups_emptied=sum(1 for q in quotas.values() if q.quota == 0),
        auc_before=n_stations_auc(index, negative_label=negative_label),
        auc_after=n_stations_auc(index, negative_label=negative_label, masks=masks),
        mean_receivers_before={k: sum(v) / len(v) for k, v in before.items() if v},
        mean_receivers_after={k: sum(v) / len(v) for k, v in after.items() if v},
        receivers_dropped=sum(q.receivers_dropped for q in quotas.values()),
        mean_station_identity_agreement=(
            sum(agreements) / len(agreements) if agreements else float("nan")
        ),
        orphan_ids=orphans,
        auc_after_excluding_orphans=n_stations_auc(
            without_orphans,
            negative_label=negative_label,
            masks=station_masks(without_orphans),
        ),
    )
