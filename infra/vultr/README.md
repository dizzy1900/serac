# Vultr compute plan — serac + rupture ($200 credit)

Status: **nothing provisioned, nothing run.** Written 2026-09-27 as a handoff. The plan below
is a proposal with its basis stated; every cost is an estimate until a run records a number.

## Blocker

No Vultr API key is configured. The owner creates it (Account → API, allow the operator's IP)
and writes it to `~/.vultr-cli.yaml` as `api-key: <key>`, mode 600. `vultr-cli` 3.11.0 is
installed on the MacBook (`brew install vultr/vultr-cli/vultr-cli`). A dedicated SSH key exists
at `~/.ssh/vultr_research_ed25519` on that machine. Before spending, check in the dashboard
whether the credit expires and whether GPU instances need a quota request.

## Plan, in order

| Step | Box | Work | Estimate | Basis |
|---|---|---|---|---|
| 1 | x86 CPU-optimised, ~8–16 vCPU | `bootstrap-cpu.sh`: first-ever Docker builds of both images, pull `openquake/engine:3.26.2` | ~$1–2 | build time only |
| 2 | same | rupture: `make validate-hazard`; `oq-classical` for Türkiye ESHM20 (fetched, never run); finish the California schedule (6 of 55 windows done; RELEASE_STATUS: 35–60 core-hours, resumable) | ~$5–8 | rupture `infra/jobs/*.yaml` |
| 3 | same | serac runout pilot: freeze a **new** design into `--reports-dir reports/runout-10k` (never overwrite the frozen 230-member ensemble in `reports/runout/`; `validate-runout` hashes it), then `serac runout run --limit 100` to measure per-member cost on Vultr vCPUs | ~$1 | measured 145 s/member mean on the 230-member run (mostly 60 m); 330 s at 30 m |
| 4 | 1–2 × 32 vCPU CPU-optimised | full 10⁴-member ensemble, resume-safe | ~$45–60 | 917 core-hours at 30 m per the manifest, ×1.5–2 for vCPU vs Apple cores; replace with the pilot's number before starting |
| 5 | 1 × L40S | `serac runout train --device cuda` on the 10⁴ ensemble | ~$10–15 | manifest: ~3 GPU-hours, extrapolated |
| 6 | Object Storage | S3-compatible DVC remote for both repos (`make dvc-remote`) | ~$6/month | replaces the local placeholder remotes |
| — | reserve | reruns, overruns | ~$100 | every estimate above is extrapolated |

Not recommended: `discriminator-train-deep` (its own manifest says compute cannot fix a
9-positive held-out fold); Sentinel-1/InSAR bulk downloads; any always-on service.

## Known issue to resolve before step 4

`reports/runout/ensemble_summary.json` carries `bytes_cap` = 3 GiB. 10⁴ members at 30 m at the
manifest's 373 KB/member is ~3.7 GB, over that cap. Decide the cap or the resolution mix when
freezing the new design, and record the decision in the freeze notes.

## Guardrails in the bootstrap

- `shutdown -h` after `MAX_HOURS` (default 72) so a forgotten box stops; a stopped Vultr
  instance **still bills** — destroy it when done.
- Build failures are recorded in `/work/build-status` rather than aborting the script.
- Restrict SSH to the operator's IP with a Vultr firewall group.
