# The event library

`data/events/` holds one reviewed JSON record per mass-movement event (contract
`MassMovementEvent`, `src/serac/domain/events.py`) and a derived GeoParquet index
`data/events/events.parquet` rebuilt by `serac events build-index`. `make validate-events`
(`serac events validate`) fails if the index drifts, if any range is unsourced, if any `best`
lacks a qualifying source, if the negative control / evacuation counterfactual / co-seismic
reference / target are missing, if the target's source volume is not `null`, or if any cited
source has no row in `data/manifest.jsonl`.

## The null-not-guess rule

Every numeric field is a `Range`. If no reliable published figure exists, the field is
`null` and `field_notes[<field>]` says why and lists the public estimates, each attributed.
A number that "looks about right" is a defect. The Langtang 2026 source volume has no
peer-reviewed estimate as of September 2026 and is stored as `null` with the one attributed
public range (ICIMOD's Farooq Azam as quoted by the Kathmandu Post) in its `FieldNote`.

Two further consequences of the rule, visible in the records:

- A figure enters a record only if the page stating it was retrieved, hashed and ledgered in
  the session that wrote the record. Abstracts are enough when the abstract states the
  figure; the full text of paywalled papers was not read and their other figures are
  `null`.
- Clock times are recorded only when a source states them. Where only a calendar date was
  read, `time.datetime_utc` is the start of that day (or the stated local period converted
  to UTC) with `uncertainty_s` covering the stated span and `basis` saying so.

## `Range`, `FieldNote`, `SourceRef` semantics (ADR-0002)

`Range`

| Field | Meaning |
|---|---|
| `low`, `high` | bounds actually stated by the referenced sources (`low <= high`) |
| `best` | a single preferred value, or `null`. Non-null **only** if some referenced source is `peer_reviewed`, `usgs_comcat`, `agency_official` or `dataset`. Press-only and conference-abstract-only ranges carry `best: null`. |
| `unit` | SI or the unit named in the field (`m`, `m3`, `km`, `m/s`, `persons`, `days`, `min`, `magnitude`) |
| `source_refs` | list (min 1) of `SourceRef.id`s in the record's `sources[]`; a list because `low` and `high` often come from different papers |
| `disputed` | `true` when published estimates disagree by more than the range can honestly express; requires `best: null`, at least two `estimates` and `notes` |
| `estimates` | `AttributedEstimate` list: value, unit, source ref, as-stated text |
| `notes` | free text, including unit conversions (e.g. km/h to m/s) and derivations from stated clock times |

`FieldNote` (required for every `Range | None` field that is `null`, including `seismic`,
`seismic.magnitude` and `seismic.agency_range`)

| Field | Meaning |
|---|---|
| `reason` | `no_peer_reviewed_estimate`, `not_applicable`, `not_yet_researched`, `disputed_beyond_range`, `not_public` |
| `public_estimates` | attributed estimates that exist but do not qualify for a `Range` |
| `notes` | free text |

Null `Range`s inside list items (`precursors_observed[i].lead_time_days`,
`transect_observations[i].arrival_time_min`, …) are explained by the item's `description`
instead of a `field_notes` entry.

`SourceRef`

| Field | Meaning |
|---|---|
| `id` | slug referenced by `source_refs` |
| `kind` | `peer_reviewed`, `preprint`, `conference_abstract`, `usgs_comcat`, `agency_official`, `dataset`, `press_report` |
| `title`, `authors`, `year`, `publisher`, `url`, `doi` | bibliographic; `doi` only if it resolved via Crossref in-session (the fetch command refuses otherwise); `url` is the final URL after redirects |
| `accessed_utc`, `sha256`, `content_type` | of the bytes actually retrieved — mandatory; a source enters a record only after a successful fetch |
| `licence`, `stored_copy` | licence as stated; a repo path for CC-BY / public-domain / ODbL copies under `data/raw/sources/<event_id>/` (gitignored, DVC-tracked once the DVC layer lands), `null` for cited-only paywalled sources |
| `claims_supported` | list of field paths in the record this source supports; every `Range` path must appear in some source's `claims_supported` (list items are claimed by leaf path, e.g. `precursors_observed[0].lead_time_days`) |
| `excerpt`, `peer_reviewed` | short quote supporting the claim; boolean |

Model validators (`MassMovementEvent._consistency`): every `source_refs` id resolves; every
`Range` path is claimed by a source; role/failure-type coupling (below); `single_force: true`
needs a peer-reviewed or ComCat source; `dammed_river` and `secondary_surge` are plain
booleans (see "Known limits").

## Citation rule

- Resolve a DOI through `https://api.crossref.org/works/<doi>` **before** writing it.
  `serac sources fetch --doi` does this and refuses to emit a `SourceRef` if the DOI does
  not resolve or resolves to a different DOI.
- Wikipedia, blogs and social media are never sources.
- Reputable press is allowed as `press_report` for 2025–2026 events only; press-only figures
  never carry `best`. Conference abstracts (`conference_abstract`) are cited but also never
  carry `best`.
- The founding brief is not a citable source. USGS ComCat geojson is a source
  (`kind: usgs_comcat`); the committed fixtures under `data/fixtures/usgs_comcat/` are cited
  with the ComCat query URL and sha256 from their ledger rows.
- Where a publisher page sits behind a bot challenge (science.org, Wiley, Taylor & Francis,
  ScienceDirect), the abstract is read from a mirror that serves it verbatim — the Europe
  PMC REST API for Science papers, the Crossref record for Wiley — and the `SourceRef.url`
  is that mirror. A paper whose abstract could not be read anywhere is not cited, even if
  its DOI resolved (Evans et al. 2009; Olivieri et al. 2022; Gnyawali et al. 2020).
- OpenStreetMap Nominatim results (ODbL) are `kind: dataset` and are used only for
  `source_location` proxies where no retrieved paper states coordinates; `basis` and
  `uncertainty_radius_m` say so.

## Workflow

1. Fetch every source you intend to cite:

   ```
   serac sources fetch URL --event ID --id SLUG --kind KIND --licence L --claims f1,f2 \
       [--doi DOI] [--store] [--apply] [--title T] [--excerpt Q] [--licence-source-url U]
   ```

   The command GETs the URL (60 s timeout, identifying user agent, redirects followed),
   hashes the bytes, resolves `--doi` through Crossref first and takes title/authors/year/
   publisher from the resolved record, optionally stores the bytes under
   `data/raw/sources/<event>/` (`--store`, only for licences that allow it), appends a
   `ManifestEntry(source=source_document, status=fetched|listed)` to `data/manifest.jsonl`,
   prints the `SourceRef` JSON and, with `--apply`, inserts it into
   `data/events/<event>.json` `sources[]`. It refuses (exit 1, nothing ledgered) on a
   non-200 response, an empty body, an unresolved DOI or a missing title (`--title`
   required for PDFs). An `--excerpt` that is not found verbatim in the bytes is a warning,
   not an error (HTML markup usually splits it).
2. Write the record: `serac events add [--repo .] [--events-dir DIR] [--force]`
   (interactive, schema-validated; every `Range` prompt demands at least one source id and
   `unknown` opens a `FieldNote` prompt; `seismic` may also be `unknown`) or
   `serac events add --from-json path` for scripted use (the only way to enter list fields
   such as `transect_observations`, `precursors_observed`, `infrastructure_impacts` and
   `related_seismic`). Both write the canonical form (sorted keys, two-space indent) and
   refuse to overwrite an existing record without `--force`; an invalid record is rejected
   with every validation message and nothing is written.
3. `serac events build-index [--repo .] [--events-dir DIR] [--out PATH]` rewrites
   `data/events/events.parquet` (EPSG:4326 source points, flattened
   `<field>_low/_high/_best` columns, `seismic_usgs_id`, `n_sources`, `json_sha256` per
   record). The suite's "index: up to date" check compares `json_sha256` with the files.
4. `serac events validate [--repo .]` (= `make validate-events`) runs the `events` suite
   (`src/serac/validation/events.py`) and writes `reports/validation/events.json`; exit 1
   on any error-severity check. Missing AOI transect files are warnings until the AOI
   branch lands.
5. `serac events report [--format table|json|markdown] [--out PATH] [--repo .]
   [--events-dir DIR] [--ledger PATH]` prints the coverage
   matrix (rows = records; columns = `s1_slc, s1_grd, hyp3_insar, s2_l2a, nisar, dem_glo30,
   era5, gacos, fdsn_waveforms, usgs_comcat, hydrometric`; windows `pre / event / post`;
   cell = best ledger status with count from `ManifestLedger.query` on `event_id` or
   `aoi_id` + window; `n/a` by dated rules such as NISAR before October 2025). The footer
   counts unresolved references and `best`-without-qualifying-source; both must be 0 or
   the command exits non-zero. Ledger rows are matched to a record by `event_id` or
   `aoi_id`; rows written without either (the ComCat and seismic fixtures fetched before
   the records existed) do not appear in the matrix.
6. Commit the record, the index, the ledger lines and any stored source copies together.

## Roles

| `role` | Meaning | Coupling enforced by the model |
|---|---|---|
| `target` | the motivating event serac is built around | — |
| `reference` | well-documented events of the same class | — |
| `negative_control` | an event serac must **discriminate from** the class | `failure_type` must be `moraine_collapse_glof` |
| `evacuation_counterfactual` | a monitored slope that was evacuated before failure; the benchmark for what L1 → L3 should enable | requires a sourced `Precursor` with `lead_time_days` and an `evacuated` infrastructure impact |
| `co_seismic_reference` | an avalanche triggered by an earthquake, not a spontaneous detachment | `failure_type` must be `co_seismic_avalanche` with the triggering `usgs_id` |

## The eleven records (as committed on 2026-09-03)

Nine items from the founding brief, held as eleven records because Aru Co and Sedongpu are
two detachments each under an `event_group`. The table is generated by hand from the
records and contains no figure that is not in them; "null" means the record carries a
`FieldNote` explaining why. `best` is shown only where the record has one.

| Record | Role | Sources (kind × n) | source_elevation_m | fall_height_m | source_volume_m3 | runout_km | peak_velocity_ms | fatalities |
|---|---|---|---|---|---|---|---|---|
| `kolka-karmadon-2002` | reference | peer_reviewed × 3 | null | 2,000 (best 2,000) | 100–130 × 10⁶ | 19–20 | null | 135–140 |
| `aru-co-2016-07` | reference | peer_reviewed × 4 | null | 800 (best 800) | 66–70 × 10⁶ (best 68 × 10⁶) | 6–8.2 | 55.6 (lower bound) | 9 (best 9) |
| `aru-co-2016-09` | reference | peer_reviewed × 4 | null | 830 (best 830) | 81–85 × 10⁶ (best 83 × 10⁶) | 7–7.2 | null | null |
| `sedongpu-2017` | reference | peer_reviewed × 2 | null | null | 17–50 × 10⁶ | null | null | null |
| `sedongpu-2018-10` | reference | peer_reviewed × 3, conference_abstract × 1 | null | 1,300 (best 1,300) | 40–150 × 10⁶ **disputed**, six attributed estimates | 8 (best 8) | null | null |
| `chamoli-2021` | reference | peer_reviewed × 5, dataset × 1 | 5,000–5,500 | 1,800 (best 1,800) | 26.9–27 × 10⁶ (best 26.9 × 10⁶) | null | null | 200 (lower bound) |
| `marmolada-2022` | reference | peer_reviewed × 1 | 3,200–3,213 (best 3,200) | null | 70,400 (best 70,400) | 2.3 (best 2.3) | 22.2–25 | 11 (best 11) |
| `south-lhonak-2023` | **negative control** | peer_reviewed × 3, dataset × 1 | null | null | 14.7–38.31 × 10⁶ | 169 (best 169) | null | 24–178 **disputed** |
| `blatten-2025` | **evacuation counterfactual** | agency_official × 2, press_report × 2, conference_abstract × 2, peer_reviewed × 1, dataset × 1 | null | null | 9–10 × 10⁶ (abstracts only, no best) | null | null | null |
| `langtang-2015` | co-seismic reference | peer_reviewed × 1, usgs_comcat × 1, agency_official × 1, dataset × 1 | 5,000 (best 5,000) | 1,900 ("nearly") | 14.38 × 10⁶ (second-hand) | null | null | 200–350 |
| `langtang-lhende-2026` | **target** | usgs_comcat × 2, agency_official × 2, press_report × 6 | 5,200 (press) | null | **null** | 100 (best 100) | null | 289–900 (press, rising) |

`rock_fraction` and `bulked_volume_m3` are `null` in every record except `langtang-2015`,
whose `bulked_volume_m3` holds the Fujita et al. (2017) deposit volume over the village.

Other per-record content:

| Record | `seismic` | Precursors | Transects | Infrastructure impacts | Notes |
|---|---|---|---|---|---|
| `kolka-karmadon-2002` | null | — | — | — | volume 100 × 10⁶ (Huggel 2005) vs 130 × 10⁶ (Kääb 2021), no best |
| `aru-co-2016-07` / `-09` | null | crevassing / terminus advance (no lead time) | — | — | coordinates, drop and reach from Kääb et al. (2021) Table 1 (stored XLSX) |
| `sedongpu-2017` | M 4.0 Chinese catalogue signal (via Kääb 2021) | — | — | — | dated by the catalogue signal, 22 Oct 2017 06:22 CST |
| `sedongpu-2018-10` | null | velocity 0.3 → 25 m/d, crevassed bulges (no lead time) | — | — | 16–18 Oct 2018 window; dammed the Yarlung Tsangpo |
| `chamoli-2021` | regional network detection; no magnitude | seismic tremors 2:30 h before (Tiwari 2022) | `raini`, `tapovan` (warning periods only) | Rishiganga, Tapovan Vishnugad damaged | onset 04:51:18 UTC (Kumar 2023) |
| `marmolada-2022` | null (earthquake trigger excluded) | meltwater overpressure since mid-June | — | — | early afternoon, clock time not read |
| `south-lhonak-2023` | null | — | — | Chungthang hydropower destroyed, 13 bridges | GLOF is the primary flood: `secondary_surge: false` |
| `blatten-2025` | press-reported magnitude 3.1, no best | evacuation order 19 May (**lead time 9 days**, Canton Valais + Federal Council), rockfall 17 May, acceleration ~2 weeks before, radar/camera velocities | — | Blatten evacuated (19 May) and destroyed (28 May), Wiler/Kippel partly evacuated | one person missing per admin.ch; no confirmed fatality count read |
| `langtang-2015` | Gorkha `us20002926` M 7.8 mww | — | — | Langtang village destroyed | time = Gorkha origin |
| `langtang-lhende-2026` | `us7000tbwb` M 5.2 ms_vx; related `us7000tc90` M 4.2 | — | `rasuwagadhi-gyirong` (null), `syabrubesi` (13 min from stated clock times, press), `betrawati` (null), `galchhi` (+9 m in 30 min, ICIMOD/press, no best) | border post, Rasuwagadhi 111 MW washed away, Chilime 22 MW buried, Upper Trishuli 3A 60 MW, Timure, Syabrubesi, 41 bridges | `initially_reported_as` "magnitude 4.4 earthquake" (ComCat text); fall height null with 1,200 m / 1,000 m attributed public figures, 2,100 m not retrievable |

## Figures seen and deliberately not entered

- The ~7.5 min (Rasuwagadhi) and ~45 min (Betrawati) arrival times for Langtang 2026 are
  recorded only as unattributed public figures in the transect descriptions: no retrieved
  source states them.
- The 2,100 m fall height for Langtang 2026 was not found on any retrievable page; the
  brief's `disputed` 1,200–2,100 m range is therefore not recorded.
- Kääb et al. (2021) quote the Sedongpu 2018 river-dam volume from Chen et al. (2020) and the
  Langtang 2015 total volume from Lacroix (2016); both are entered as attributed estimates
  with no `best`.
- Later Nepali death tolls (>1,000) appeared in search snippets from outlets that are not
  on the allowed press list and were not entered.

## Known limits

- `dammed_river` and `secondary_surge` are non-nullable booleans in the contract. Where the
  retrieved sources do not state them they are recorded as `false` and the record's `notes`
  say that `false` means "not established in the retrieved text". A nullable tri-state would
  be more honest; that is a contract change for the domain-modeller of the next phase.
- Coordinates for Kolka, Aru and Sedongpu come from Kääb et al. (2021) Table 1 (0.01°
  precision); Chamoli, South Lhonak, Blatten and Langtang 2015 use OpenStreetMap features
  as proxies with 1.5–3 km radii.
- `data/raw/sources/` is gitignored; the committed records carry only the hashes, and the
  ledger rows carry the paths. Re-fetching a stored source is expected to change the hash of
  dynamic pages.
