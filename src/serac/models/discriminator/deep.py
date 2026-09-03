"""Compact CNN + station-axis transformer, and the promotion rule it has to clear.

Architecture, and the reason for each part:

* a **shared 1-D CNN** over each receiver's three components, strided down from 12000 samples
  to a 64-dimensional embedding. Shared, so the model has one encoder for "what a seismogram
  looks like" rather than twelve.
* a **transformer over the station axis with no positional encoding**. This is the load-bearing
  design decision. Windows arrive with their receivers in an arbitrary slot order, and adding a
  positional encoding would let the model learn "slot 0 is the nearest receiver", which is
  epicentral distance re-entering through the back door. Without one the encoder is
  permutation-invariant by construction, so it can only pool evidence across receivers, which
  is what a multi-receiver discriminator should do.
* a **masked mean pool** and a three-way head. Padded slots are masked in attention and in the
  pool, so an event with four receivers is not diluted by eight zero rows.

`import seisbench.models` is forbidden and a grep test enforces it. SeisBench's models are
phase pickers: EQTransformer, PhaseNet and GPD are trained to place P and S arrivals, not to
say what kind of source made them. Loading one and reading its output as a class score would
be a category error dressed up as transfer learning. SeisBench's **data** side is used instead
(`seisbench.generate.Normalize` for the per-trace normalisation), which is what the brief asks.

**Promotion is decided before training.** The deep model becomes the default only if the
paired-bootstrap 95% lower bound on delta F1 against the baseline exceeds zero. The rule is in
`evaluate.paired_bootstrap` and in this docstring so it cannot be renegotiated after a result
is seen. At roughly twenty held-out positives the honest expectation is that the comparison is
inconclusive and the baseline stays, and an inconclusive result is reported as the finding.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import numpy as np
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from serac.errors import SeracError
from serac.models.discriminator.baseline import CLASSES, group_hash
from serac.models.discriminator.windows import COMPONENTS, MAX_STATIONS_PER_EVENT

if TYPE_CHECKING:  # pragma: no cover
    import torch
    from torch import nn

DEEP_VERSION = "0.1.0"
DEEP_NAME = "cnn-station-transformer"

ARTIFACT_DIR: Final = Path("baselines/discriminator_deep")
WEIGHTS_FILE: Final = "weights.pt"
DEEP_ARTIFACT_FILE: Final = "artifact.json"

EMBED_DIM: Final = 64
N_HEADS: Final = 4
N_TRANSFORMER_LAYERS: Final = 2
DROPOUT: Final = 0.2

EPOCHS: Final = 40
BATCH_SIZE: Final = 16
LEARNING_RATE: Final = 3e-4
WEIGHT_DECAY: Final = 1e-4
PATIENCE: Final = 8
SEED: Final = 20260903

# The promotion rule, stated once and referenced everywhere.
PROMOTION_RULE: Final = (
    "The deep model becomes the default only if the paired-bootstrap 95% lower bound on "
    "delta F1 against the lightgbm baseline, resampled over test event groups, exceeds 0. "
    "Fixed before either model was trained."
)


class WindowSource(Protocol):
    """Row-at-a-time access to the window store.

    A plain `np.ndarray` satisfies this, and so does a lazy Zarr-backed reader. The deep model
    only ever needs one window at a time, so it does not require the whole store in memory.
    """

    shape: tuple[int, ...]

    def __getitem__(self, key: Any) -> np.ndarray: ...


class DeepError(SeracError):
    """The deep model could not be built, trained or loaded."""


class DeepArtifact(BaseModel):
    """What the deep run was, so it can be compared and reproduced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deep_version: str = DEEP_VERSION
    name: str = DEEP_NAME
    trained_at_utc: AwareDatetime
    split_scheme: str
    n_parameters: int = Field(ge=0)
    epochs_run: int = Field(ge=0)
    best_epoch: int = Field(ge=0)
    best_val_macro_f1: float
    n_train_windows: int = Field(ge=0)
    n_val_windows: int = Field(ge=0)
    train_event_groups_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    device: str
    hyperparameters: dict[str, Any]
    promotion_rule: str = PROMOTION_RULE
    notes: list[str] = Field(default_factory=list)


def normalise(waveform: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Per-trace peak normalisation, the `seisbench.generate.Normalize` "peak" convention.

    Absolute amplitude is deliberately discarded. It is dominated by source size and
    epicentral distance, and letting the network see it would reintroduce the geometry the
    feature set is careful to exclude. What is left is waveform *shape*, which is the thing
    that separates a single force from a double couple.
    """
    out = np.asarray(waveform, dtype=np.float32).copy()
    for slot in range(out.shape[0]):
        for component in range(out.shape[1]):
            if not bool(valid[slot, component]):
                out[slot, component] = 0.0
                continue
            trace = out[slot, component]
            trace -= trace.mean()
            peak = float(np.abs(trace).max())
            out[slot, component] = trace / peak if peak > 0 else 0.0
    return out


def build_model() -> nn.Module:
    """The network. Built lazily so importing this module does not require torch."""
    import torch
    from torch import nn

    class TraceEncoder(nn.Module):
        """Shared 1-D CNN: (B*S, 3, 12000) -> (B*S, EMBED_DIM)."""

        def __init__(self) -> None:
            super().__init__()
            channels = [len(COMPONENTS), 16, 24, 32, 48, EMBED_DIM]
            layers: list[nn.Module] = []
            for inp, out in pairwise(channels):
                layers += [
                    nn.Conv1d(inp, out, kernel_size=9, stride=4, padding=4),
                    nn.BatchNorm1d(out),
                    nn.GELU(),
                ]
            self.body = nn.Sequential(*layers)
            self.pool = nn.AdaptiveAvgPool1d(1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            out: torch.Tensor = self.pool(self.body(x)).squeeze(-1)
            return out

    class Discriminator(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = TraceEncoder()
            layer = nn.TransformerEncoderLayer(
                d_model=EMBED_DIM,
                nhead=N_HEADS,
                dim_feedforward=EMBED_DIM * 2,
                dropout=DROPOUT,
                batch_first=True,
                norm_first=True,
                activation="gelu",
            )
            # No positional encoding is added anywhere: the station axis carries no order.
            self.transformer = nn.TransformerEncoder(layer, num_layers=N_TRANSFORMER_LAYERS)
            self.head = nn.Sequential(
                nn.LayerNorm(EMBED_DIM),
                nn.Dropout(DROPOUT),
                nn.Linear(EMBED_DIM, len(CLASSES)),
            )

        def forward(self, x: torch.Tensor, slot_valid: torch.Tensor) -> torch.Tensor:
            batch, slots = x.shape[0], x.shape[1]
            embedded = self.encoder(x.reshape(batch * slots, x.shape[2], x.shape[3]))
            embedded = embedded.reshape(batch, slots, EMBED_DIM)
            padding = ~slot_valid
            # A row with no valid slot would make every key masked and produce NaNs; keep its
            # first slot unmasked and let the masked pool below zero its contribution instead.
            all_padded = padding.all(dim=1)
            padding = padding.clone()
            padding[all_padded, 0] = False
            encoded = self.transformer(embedded, src_key_padding_mask=padding)
            weights = (~padding).float().unsqueeze(-1)
            pooled = (encoded * weights).sum(dim=1) / weights.sum(dim=1).clamp(min=1.0)
            logits: torch.Tensor = self.head(pooled)
            return logits

    torch.manual_seed(SEED)
    return Discriminator()


def n_parameters(model: nn.Module) -> int:
    return int(sum(p.numel() for p in model.parameters() if p.requires_grad))


def select_device() -> str:
    """MPS when available, else CPU. There is no CUDA on this machine and none is assumed."""
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def train(
    waveforms: WindowSource,
    valids: np.ndarray,
    labels: np.ndarray,
    splits: np.ndarray,
    groups: list[str],
    *,
    split_scheme: str,
    artifact_dir: Path,
    epochs: int = EPOCHS,
    progress: Any = None,
) -> DeepArtifact:
    """Train on train, early-stop on validation macro F1, never touch test."""
    from datetime import UTC, datetime

    import torch
    from torch import nn

    from serac.models.discriminator.evaluate import _prf

    if (splits == "test").any() and not (splits == "train").any():
        raise DeepError("no training rows; refusing to train on a test-only split")

    device = torch.device(select_device())
    model = build_model().to(device)
    torch.manual_seed(SEED)

    is_train = splits == "train"
    is_val = splits == "val"
    if not is_train.any() or not is_val.any():
        raise DeepError(f"scheme {split_scheme}: train and val folds must both be non-empty")

    # Batches are normalised on demand rather than materialised up front. The full training
    # tensor would be ~2.6 GB of float32 on a 16 GB machine shared with three other tracks,
    # and the resulting swap made an epoch unbounded; this keeps peak resident memory to one
    # batch at a time.
    train_rows = np.flatnonzero(is_train)
    val_rows = np.flatnonzero(is_val)

    def batch(rows: np.ndarray) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        x = np.stack([normalise(waveforms[i], valids[i]) for i in rows]).astype(np.float32)
        slot_valid = valids[rows].any(axis=2)
        return (
            torch.from_numpy(x),
            torch.from_numpy(slot_valid.astype(bool)),
            torch.from_numpy(labels[rows].astype(np.int64)),
        )

    counts = np.bincount(labels[is_train].astype(int), minlength=len(CLASSES)).astype(np.float64)
    weight = torch.tensor(
        [(labels[is_train].size / (len(CLASSES) * c) if c > 0 else 0.0) for c in counts],
        dtype=torch.float32,
        device=device,
    )
    criterion = nn.CrossEntropyLoss(weight=weight)
    optimiser = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    generator = torch.Generator().manual_seed(SEED)

    best_f1, best_epoch, best_state, since_best = -1.0, 0, None, 0
    for epoch in range(1, epochs + 1):
        model.train()
        order = torch.randperm(train_rows.size, generator=generator).numpy()
        total = 0.0
        for start in range(0, order.size, BATCH_SIZE):
            rows = train_rows[order[start : start + BATCH_SIZE]]
            x, slot_valid, y = batch(rows)
            optimiser.zero_grad()
            logits = model(x.to(device), slot_valid.to(device))
            loss = criterion(logits, y.to(device))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimiser.step()
            total += float(loss.item()) * rows.size

        model.eval()
        chunks: list[np.ndarray] = []
        truths: list[np.ndarray] = []
        with torch.no_grad():
            for start in range(0, val_rows.size, BATCH_SIZE):
                rows = val_rows[start : start + BATCH_SIZE]
                x, slot_valid, y = batch(rows)
                chunks.append(
                    model(x.to(device), slot_valid.to(device)).argmax(dim=1).cpu().numpy()
                )
                truths.append(y.numpy())
        predicted = np.concatenate(chunks)
        y_true = np.concatenate(truths)
        macro = float(np.mean([_prf(y_true, predicted, i)[2] for i in range(len(CLASSES))]))
        if progress is not None:
            progress(
                f"epoch {epoch}: train loss {total / max(1, order.size):.4f}, "
                f"val macro F1 {macro:.3f}"
            )
        if macro > best_f1:
            best_f1, best_epoch, since_best = macro, epoch, 0
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        else:
            since_best += 1
            if since_best >= PATIENCE:
                break

    if best_state is not None:
        model.load_state_dict(best_state)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), artifact_dir / WEIGHTS_FILE)

    train_groups = sorted({g for g, keep in zip(groups, is_train, strict=True) if keep})
    artifact = DeepArtifact(
        trained_at_utc=datetime.now(tz=UTC),
        split_scheme=split_scheme,
        n_parameters=n_parameters(model),
        epochs_run=epoch,
        best_epoch=best_epoch,
        best_val_macro_f1=best_f1,
        n_train_windows=int(is_train.sum()),
        n_val_windows=int(is_val.sum()),
        train_event_groups_sha256=group_hash(train_groups),
        device=str(device),
        hyperparameters={
            "embed_dim": EMBED_DIM,
            "n_heads": N_HEADS,
            "n_layers": N_TRANSFORMER_LAYERS,
            "dropout": DROPOUT,
            "batch_size": BATCH_SIZE,
            "learning_rate": LEARNING_RATE,
            "weight_decay": WEIGHT_DECAY,
            "patience": PATIENCE,
            "seed": SEED,
            "positional_encoding": False,
            "max_stations": MAX_STATIONS_PER_EVENT,
        },
        notes=[
            "no positional encoding on the station axis: the encoder is permutation-invariant "
            "so it cannot key on slot order, which would be epicentral distance by proxy",
            "per-trace peak normalisation discards absolute amplitude, which is dominated by "
            "source size and distance",
            "seisbench.models is never imported; those are phase pickers, not classifiers",
        ],
    )
    (artifact_dir / DEEP_ARTIFACT_FILE).write_text(
        artifact.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return artifact


def predict(
    waveforms: WindowSource, valids: np.ndarray, artifact_dir: Path
) -> tuple[np.ndarray, np.ndarray]:
    """(predicted class indices, softmax probabilities) from committed weights."""
    import torch

    model = build_model()
    model.load_state_dict(torch.load(artifact_dir / WEIGHTS_FILE, map_location="cpu"))
    model.eval()
    slot_valid_all = valids.any(axis=2).astype(bool)
    outputs = []
    with torch.no_grad():
        for start in range(0, waveforms.shape[0], 16):
            rows = range(start, min(start + 16, waveforms.shape[0]))
            x = torch.from_numpy(
                np.stack([normalise(waveforms[i], valids[i]) for i in rows]).astype(np.float32)
            )
            outputs.append(model(x, torch.from_numpy(slot_valid_all[start : start + 16])))
    logits = torch.cat(outputs)
    probabilities = torch.softmax(logits, dim=1).numpy()
    return probabilities.argmax(axis=1), probabilities
