# Contributing to serac

serac is a standalone open-source project (Apache-2.0). It has no parent organisation and
inherits no external conventions; everything it needs is defined in this repository.

## Ground rules

1. **No fabricated data.** If a dataset cannot be fetched, record it as `status: not_fetched`
   in `data/manifest.jsonl` and fail loudly at runtime. Synthetic data is allowed only when
   labelled `provenance: synthetic`, and only under `tests/fixtures/synthetic/`.
2. **Unknowns are `null`, not guesses.** Every numeric field in the event library is a `Range`
   with `source_refs`; a `null` needs a `field_notes` entry explaining why.
3. **Provenance on every record**: source URL, retrieval timestamp, checksum, licence.
4. **Tests run offline.** `make test` blocks network access. Network behaviour lives behind the
   `online` marker and `make smoke-online`, which is allowed to skip.
5. **Small commits, conventional messages, green tree at every commit.**

## Workflow

```bash
# macOS only: lightgbm needs the OpenMP runtime, which is not a Python package.
#   brew install libomp
uv sync --all-extras
make lint typecheck test
make validate-serac
```

Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
`TODO` comments must reference an issue or a numbered entry in `RELEASE_STATUS.md`.

Downstream consumers integrate through the JSON Schemas in `contracts/`, never by importing
`serac` internals. Changing a contract requires bumping its version and regenerating the schema
with `serac schema export`.
