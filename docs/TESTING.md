# Testing AgentCMS

How the test suite runs, what it needs, and the exact gate CI runs — so a
human or the next agent can reproduce the push gate locally.

## What the suite is

`tests/` is a pytest suite (706 tests at this revision, commit `24b6870`) that
exercises the API through `TestClient`, the data model, migrations, and the
operational tooling. Every test runs against a **throwaway PostgreSQL server**:
the whole server is created for the session and destroyed afterwards, and each
test starts from a truncated schema, so tests cannot leak state (`tests/conftest.py`,
`tests/pg.py`).

## How the ephemeral Postgres is chosen

`tests/pg.py::start_ephemeral_postgres` picks a strategy in this order:

1. **`TEST_DATABASE_URL`** (environment) — a server provided by the caller
   (CI's `postgres:16` service container). The suite still creates and drops
   its own databases on it.
2. **Docker** — a throwaway `postgres:16-alpine` container on a free port
   (the version the project targets).
3. **`initdb`/`pg_ctl`** — a throwaway cluster from a local Postgres install,
   so the suite works on a laptop with no Docker daemon.

You can force an empty fallback for yourself with
`AGENTCMS_TEST_NO_DOCKER=1`.

Run it:

```text
$ pytest -q
706 passed, 2 warnings in 40.38s
```

(The two lines above are real, from `make test` against a docker-backed
ephemeral Postgres 16 at this revision.)

`make test` is the equivalent one-command target:

```text
$ make test
… (above) …
======================= 706 passed, 2 warnings in 40.38s =======================
```

## About "the SQLite fallback" — there is none, deliberately

Some API suites flip `DATABASE_URL=sqlite://…` to run "without a database".
**This project does not have that fallback, and that is intentional:**

- the migrations (`alembic/`) target PostgreSQL (JSONB-ish columns, arrays,
  full-text search with `tsvector`/trigram, `gen_random_uuid`, `CREATE DATABASE`
  fixtures in `tests/pg.py`);
- the conftest machinery (`truncate`, ephemeral servers, per-test isolation)
  is built around Postgres;
- a SQLite run would quietly *miss* migration/full-text branches without
  failing loudly, which is worse than failing loudly.

**When a SQLite fallback would be legitimate:** only in a hypothetical minimal
subset that genuinely does not use Postgres features (a read-only, no-fts smoke
suite). It is **not legitimate** as a stand-in for the migration gate or the
content API tests, and `DATABASE_URL=sqlite://…` will not work here. Run the
real thing.

## The CI-mirroring gate (copy from `.github/workflows/ci.yml`)

CI's `lint`, `migrations` and `test` jobs boil down to this single command
chain. **This exact chain was executed at this revision, green end to end:**

```sh
ruff format --check . && ruff check . && mypy && pytest -q
```

```text
$ ruff format --check . && ruff check . && mypy && pytest -q
148 files already formatted
All checks passed!
Success: no issues found in 92 source files
706 passed, 2 warnings in 40.75s
```

And, against Postgres 16 (the `migrations` job is upgrade → downgrade → upgrade
→ drift check on a real `postgres:16`):

```sh
# and, against Postgres 16:
alembic upgrade head && alembic downgrade base && alembic upgrade head && alembic check
```

```text
$ DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:15433/agentcms \
    alembic upgrade head && \
    alembic downgrade base && \
    alembic upgrade head && \
    alembic check
INFO  [alembic.runtime.migration] Running upgrade … -> f4e5d6c7b8a9, Add request_id -> webhook_deliveries (#24).   (upgrade head)
INFO  [alembic.runtime.migration] Running downgrade … -> , create_all_tables                                      (downgrade base)
INFO  [alembic.runtime.migration] Running upgrade … -> f4e5d6c7b8a9, Add request_id -> webhook_deliveries (#24).   (upgrade head)
No new upgrade operations detected.                                                                              (alembic check)
```

`make gate` runs the lint + test half; `make gate-migrations` runs the
destructive migrations half against whatever `DATABASE_URL` points at:

```bash
make gate              # ruff check . && ruff format --check . && mypy, then pytest
make gate-migrations   # alembic upgrade head && downgrade base && upgrade head && check  ⚠️ DESTRUCTIVE
```

## What "red on main" meant (#25) and where it stands

When this ticket was written, `main` was red on CI with two known failures:

1. `ruff format .` drift;
2. `alembic check` — models and migrations had drifted apart.

Both were fixed in this push (`ruff format .` applied, and the model↔migration
drift in the same revision), and the full gate above is **green at this
revision** — see the outputs pasted here. There is no remaining "known red"
note to promise.

While making the gate green, three further real defects that only show up when
you *actually run* the documented commands were found and fixed in this push
(full story in `NOTES-opencode.md`):

- `scripts/restore_drill.py` built the scratch-database URL with `str(URL)`,
  which SQLAlchemy renders with a **masked password** (`***`) — the restore
  drill failed with `password authentication failed` against any
  password-authed server (CI's `postgres:16` service container, or the docker
  ephemeral path). Only the `initdb`/trust path looked green before.
- `scripts/pgbackup.py` restored through `pg_restore -d`, which since PG 17
  unconditionally emits `SET transaction_timeout = 0;` — a parameter that only
  exists on PG ≥ 17, so a modern client restoring into a Postgres 16 server
  broke. The restore now generates SQL via `pg_restore -f -`, strips that one
  housekeeping line, and replays through `psql -v ON_ERROR_STOP=1`.
- `pyproject.toml` listed `httpx` as a **dev-only** extra even though runtime
  modules import it — the production image could not boot. It is now a core
  dependency.

## Rules of thumb

- Never run the gate against a database with data you care about
  (`gate-migrations` wipes `$DATABASE_URL`).
- The suite's Postgres is ephemeral and disposer-of-self; a leftover
  `agentcms-pgdata-*` directory or `agentcms-test-pg-*` container from a
  crashed run can be deleted freely, and the next run creates its own.
- `pytest -q` vs `make test` are the same suite; `make test` additionally
  guarantees the venv + install are up to date first.