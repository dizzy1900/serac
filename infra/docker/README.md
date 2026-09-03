# infra/docker — local development dependencies

`compose.yaml` provides the services the serac streaming lane needs locally:

| Service | Image | Purpose | Status |
|---|---|---|---|
| `redis` | `redis:7-alpine` | Redis Streams bus (`RedisStreamsBus`, ADR-0007); named volume `serac-redis-data`; healthcheck `redis-cli ping` | file written, **not run** |
| `grass` | — | GRASS GIS placeholder for Prompt 2; commented out | placeholder |

**This compose file is untested.** The machine that wrote it has no Docker installed
(`RELEASE_STATUS.md` Known gaps #7). The Redis bus adapter is unit-tested only against
`fakeredis`; the live-server test (`redis` marker) has never run. The first person with
Docker should run the steps below and flip the `RedisStreamsBus` and `Docker Compose` rows in
`RELEASE_STATUS.md` on success.

## Bring-up

```bash
docker compose -f infra/docker/compose.yaml up -d
docker compose -f infra/docker/compose.yaml ps        # redis should be "healthy"
```

Redis is bound to `127.0.0.1:6379` only. Persistence is append-only (`--appendonly yes`) on
the named volume so stream contents survive a restart; `docker compose down -v` wipes it.

## Running the online / Redis tests against it

```bash
cp .env.example .env                       # if you have not already
# SERAC_REDIS_URL defaults to redis://localhost:6379/0 in .env.example
SERAC_REDIS_URL=redis://localhost:6379/0 make smoke-online
```

`make smoke-online` sets `SERAC_ONLINE=1` and runs tests marked `online` or `redis`. The
`redis`-marked tests are collected only when `SERAC_REDIS_URL` is set and the server answers
`PING` (`tests/conftest.py`); otherwise they skip. Network-dependent `online` tests skip when
their host is unreachable. Skips are a valid outcome and never turn CI red.

To run the replay against the Redis bus instead of the in-memory bus (planned CLI flag):

```bash
uv run serac replay --event chamoli-2021 --speed max --bus redis
```

## Tear-down

```bash
docker compose -f infra/docker/compose.yaml down        # keep the volume
docker compose -f infra/docker/compose.yaml down -v     # drop stream data
```

## Deployment image

The deployment unit for serac is a plain Docker image containing the `serac` package
(ADR-0014). Its `Dockerfile` is not part of Prompt 1; this directory holds development
dependencies only.
