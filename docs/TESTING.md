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

## Locale requirement for `initdb` (#34)

PostgreSQL 18+ refuses to run `initdb` when `LANG`, `LC_ALL`, and `LC_CTYPE`
are **all unset** — exactly the environment you get from `systemd`, `launchd`,
`docker exec`, or a minimal container without a login shell. The symptom is:

```text
initdb: error: invalid locale settings; check LANG and LC_* environment variables
```

**Fix in this repo:** every internal `initdb` call site (`tests/pg.py`,
`scripts/pgbackup.py`) now passes an explicit locale environment that defaults
to `C.UTF-8`:

```python
# tests/pg.py::_locale_env()  — used by start_initdb_postgres()
# scripts/pgbackup.py::_locale_env()  — used by init_scratch_cluster()


def _locale_env() -> dict[str, str]:
    env = dict(os.environ)
    for var, default in (("LANG", "C.UTF-8"), ("LC_ALL", "C.UTF-8"), ("LC_CTYPE", "C.UTF-8")):
        env.setdefault(var, default)
    return env
```

**For self-hosters:** if you run the test suite or the restore drill in a
container/CI job without a locale, you don't need to do anything — the helpers
pin `C.UTF-8` automatically. If you invoke `initdb`/`pg_ctl` directly in your
own scripts, set `LANG=C.UTF-8 LC_ALL=C.UTF-8 LC_CTYPE=C.UTF-8` (or any valid
UTF-8 locale) before the call.

**CI regression test:** the `test-locale-scrubbed` job in
`.github/workflows/ci.yml` runs `pytest -q` under `env -u LANG -u LC_ALL -u
LC_CTYPE -u LANGUAGE` and must stay green.

## Dependency lock

CI does **not** resolve dependencies from `pyproject.toml` on every run. It installs
the pinned set from `requirements.lock.txt`:

```text
python -m pip install --no-deps -r requirements.lock.txt
python -m pip install --no-deps -e .
```

Why: on 2026-09-25 a routine CI run resolved SQLAlchemy **2.1.0**, which removed
`sqlalchemy.ext.mypy.plugin` while `[tool.mypy].plugins` still required it, and the
`lint` job died at startup with

```text
pyproject.toml:1: error: Error importing plugin "sqlalchemy.ext.mypy.plugin":
No module named 'sqlalchemy.ext.mypy'  [misc]
Found 1 error in 1 file (errors prevented further checking)
```

— before checking a single file. The gate was unpinnable, so it could be (and was)
broken by a dependency release nobody chose.

**Resolution (#41).** The plugin is gone from `[tool.mypy].plugins`: SQLAlchemy 2.0+
types the ORM inline (`Mapped[...]`/`mapped_column()`), so the shim bought us nothing,
and 2.1 deleted the module outright. `mypy` is green on **2.1.0** with no `# type:
ignore` added, the `<2.1` cap from #40 is relaxed to `<3`, and `requirements.lock.txt`
pins `sqlalchemy==2.1.0` — CI now tests the exact version that broke it. #40 pinned
the closure; #41 removed the dependence on a module a bump can delete.

The contract now:

- `pyproject.toml` holds the **ranges** (with upper bounds on the critical deps);
  `requirements.lock.txt` holds the exact pins of the full closure, runtime + dev.
- `python scripts/check_lock.py` fails if a dependency declared in `pyproject.toml`
  is missing from the lock or is pinned outside its specifier. CI's `lint` job runs
  it, and so does `make lint` / `make check-lock`.
- Regenerate with `make lock` after editing `pyproject.toml`, and commit the lock in
  the same change — the lock diff is the review of what CI will test. To move **one**
  pin without churning the rest (the monthly dependency bump), use
  `pip-compile --extra dev --strip-extras --output-file requirements.lock.txt --upgrade-package <name> pyproject.toml`;
  `make lock` re-resolves everything.
- `tests/test_dependency_contract.py` asserts the invariants inside the pytest suite,
  so a broken gate fails the `test` job too: the lock pins every component
  `pyproject.toml` declares, every declared mypy plugin imports, no mypy plugin is
  declared that a supported dependency release has deleted — and
  `test_the_type_gate_actually_checks_files` points `mypy` at a canary file with a
  deliberate type error and requires the error back. A config that merely *declares*
  strictness is not evidence that anything was checked.

## Rules of thumb

- Never run the gate against a database with data you care about
  (`gate-migrations` wipes `$DATABASE_URL`).
- The suite's Postgres is ephemeral and disposer-of-self; a leftover
  `agentcms-pgdata-*` directory or `agentcms-test-pg-*` container from a
  crashed run can be deleted freely, and the next run creates its own.
- `pytest -q` vs `make test` are the same suite; `make test` additionally
  guarantees the venv + install are up to date first.