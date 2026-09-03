# The event library

`data/events/` holds one reviewed JSON record per mass-movement event (contract
`MassMovementEvent`, `src/serac/domain/events.py`, planned) and a derived GeoParquet index
`data/events/events.parquet` rebuilt by `serac events build-index`. `make validate-events`
fails if the index drifts, if any range is unsourced, if any `best` lacks a qualifying source,
or if the negative control is missing.

## The null-not-guess rule

Every numeric field is a `Range`. If no reliable published figure exists, the field is
`null` and `field_notes[<field>]` says why and lists the public estimates, each attributed.
A number that "looks about right" is a defect. The Langtang 2026 source volume has no
peer-reviewed estimate as of September 2026 and is stored as `null` with attributed public
estimates in its `FieldNote`.

## `Range`, `FieldNote`, `SourceRef` semantics (ADR-0002)

`Range`

| Field | Meaning |
|---|---|
| `low`, `high` | bounds actually stated by the referenced sources (`low <= high`) |
| `best` | a single preferred value, or `null`. Non-null **only** if some referenced source is `peer_reviewed`, `usgs_comcat`, `agency_official` or `dataset`. Press-only ranges carry `best: null`. |
| `unit` | SI or the unit named in the field (`m`, `m3`, `km`, `m/s`, `MW`) |
| `source_refs` | list (min 1) of `SourceRef.id`s in the record's `sources[]`; a list because `low` and `high` often come from different papers |
| `disputed` | `true` when published estimates disagree by more than the range can honestly express; requires `best: null`, at least two `estimates` and `notes` |
| `estimates` | `AttributedEstimate` list: value, unit, source ref, as-stated text |
| `notes` | free text |

`FieldNote` (required for every `Range | None` field that is `null`)

| Field | Meaning |
|---|---|
| `reason` | `no_peer_reviewed_estimate`, `not_applicable`, `not_yet_researched`, `disputed_beyond_range`, `not_public` |
| `public_estimates` | attributed estimates that exist but do not qualify for a `Range` |
| `notes` | free text |

`SourceRef`

| Field | Meaning |
|---|---|
| `id` | slug referenced by `source_refs` |
| `kind` | `peer_reviewed`, `usgs_comcat`, `agency_official`, `dataset`, `press_report`, … |
| `title`, `authors`, `year`, `url`, `doi` | bibliographic; `doi` only if it resolved via Crossref or the publisher in-session |
| `accessed_utc`, `sha256`, `content_type` | of the bytes actually retrieved — mandatory; a source enters a record only after a successful fetch |
| `licence`, `stored_copy` | licence as stated; a repo path for CC-BY / public-domain copies, `null` for cited-only paywalled sources |
| `claims_supported` | list of field paths in the record this source supports; every `Range` path must appear in some source's `claims_supported` |
| `excerpt`, `peer_reviewed` | short quote supporting the claim; boolean |

Model validators (planned): every `source_refs` id resolves; every `Range` path is claimed
by a source; role/failure-type coupling (below); `single_force: true` needs a peer-reviewed
or ComCat source.

## Citation rule

- Resolve a DOI through `https://api.crossref.org/works/<doi>` or the publisher page
  **before** writing it. Unresolved → not cited.
- Wikipedia, blogs and social media are never sources.
- Reputable press is allowed as `press_report` for 2025–2026 events only; press-only figures
  never carry `best`.
- The founding brief is not a citable source. USGS ComCat geojson is a source
  (`kind: usgs_comcat`).
- `serac sources fetch URL --event ID --id SLUG --kind KIND --licence L --claims f1,f2
  [--doi DOI]` (planned) performs the GET, hashes the bytes, stores CC-BY / public-domain
  copies (USGS geojson as committed fixtures; CC-BY PDFs under DVC-tracked
  `data/raw/sources/`), never stores paywalled PDFs, appends a
  `ManifestEntry(source=source_document)` and emits the `SourceRef` to paste into the
  record.

## Roles

| `role` | Meaning | Coupling enforced by the model |
|---|---|---|
| `target` | the motivating event serac is built around | — |
| `reference` | well-documented events of the same class | — |
| `negative_control` | an event serac must **discriminate from** the class | `failure_type` must be `moraine_collapse_glof` |
| `evacuation_counterfactual` | a monitored slope that was evacuated before failure; the benchmark for what L1 → L3 should enable | requires a sourced `Precursor` with `lead_time_days` and an `evacuated` infrastructure impact |
| `co_seismic_reference` | an avalanche triggered by an earthquake, not a spontaneous detachment | `failure_type` must be `co_seismic_avalanche` with the triggering `usgs_id` |

## Adding an event

1. Fetch every source you intend to cite with `serac sources fetch …` (planned). Each fetch
   produces a `SourceRef` and a ledger line.
2. Run `serac events add` (interactive, schema-validated; `--from-json path` for scripted
   use). Enter `null` for anything you cannot source; the prompt will ask for the
   `FieldNote`.
3. Run `serac events build-index` then `make validate-events`.
4. Run `serac events report` to see the coverage matrix (rows = records; columns =
   `s1_slc, s1_grd, hyp3_insar, s2_l2a, nisar, dem_glo30, era5, gacos, fdsn_waveforms,
   usgs_comcat, hydrometric`; windows `pre / event / post`; cell = best manifest status with
   count; `n/a` by dated rules such as NISAR before Oct 2025). The footer counts unresolved
   references and `best`-without-qualifying-source; both must be 0 or the command exits
   non-zero.
5. Commit the record, the index, the ledger lines and any stored source copies together.

## Seeded events (names and dates only; figures live in the records with their sources)

Nine items from the founding brief, held as eleven records because Aru Co and Sedongpu are
two detachments each under an `event_group`:

| # | Event | Date(s) | Record id(s) | Role |
|---|---|---|---|---|
| 1 | Kolka–Karmadon, Russia | 20 Sep 2002 | `kolka-karmadon-2002` | reference |
| 2 | Aru Co twin glacier detachments, Tibet | 17 Jul and 21 Sep 2016 | `aru-co-2016-07`, `aru-co-2016-09` | reference |
| 3 | Sedongpu, Tibet | 2017 and Oct 2018 | `sedongpu-2017-10`, `sedongpu-2018-10` | reference; volume recorded as `disputed` with each published figure attributed |
| 4 | Chamoli / Ronti Peak, India | 7 Feb 2021 | `chamoli-2021` | reference (best-documented; replay/backtest event) |
| 5 | Marmolada, Italy | 3 Jul 2022 | `marmolada-2022` | reference |
| 6 | South Lhonak, Sikkim | 3 Oct 2023 | `south-lhonak-2023` | **negative control** (`moraine_collapse_glof`) |
| 7 | Birch Glacier / Blatten, Switzerland | 28 May 2025 | `blatten-2025` | **evacuation counterfactual** (monitored, evacuated before failure) |
| 8 | Langtang village co-seismic avalanche, Nepal | 25 Apr 2015 | `langtang-2015` | co-seismic reference (`co_seismic_avalanche`) |
| 9 | Langtang Lirung / Lhende Khola, Nepal–Tibet | 26 Aug 2026 | `langtang-lhende-2026` | **target**; USGS `us7000tbwb` and `us7000tc90`; source volume `null` |

Record ids are the planned identifiers; the domain-modeller branch owns their content.
