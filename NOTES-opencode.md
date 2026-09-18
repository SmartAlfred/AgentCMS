# NOTES-opencode.md — static export (ticket #23)

## What was delivered

- `app/services/export.py` — deterministic static export engine.
- `app/api/v1/export.py` + registration in `app/api/v1/routes.py` —
  `GET /v1/sites/{site}/export?format=tar.gz|zip&since=<iso>`.
- `scripts/cli.py` + `[project.scripts] agentcms` entry — `agentcms export`,
  standalone `--verify`, `--incremental`, `--since`, and `--deploy=github-pages`
  plan printing / dry-run.
- `tests/test_export.py` — 27 tests covering every acceptance criterion.
- `docs/ci/github-pages.yml` — workflow that rebuilds and publishes to the
  `gh-pages` branch on `workflow_dispatch` and `repository_dispatch`
  (`post.published` webhook event) — the pull-based hosting path for the
  publish webhook (#21).
- `docs/api.md` — export endpoint + `manifest.json` schema documentation.

## Key decisions

- **Determinism**: no wall-clock values enter output. Feed `generated_at`
  derives from the newest post `updated_at` (overridable via
  `--generated-at`); `canonical_json` is compact+sorted+`ensure_ascii=False`;
  archives sort members, pin tar metadata (GNU_FORMAT, `mtime=0`, uid/gid 0,
  `mode 0644`), gzip with `mtime=0`; ZIP uses `date_time=(1980,1,1,…)`.
  Two runs on the same database produce byte-identical archives.
- **HTML parity**: post/index/tag pages render through the exact same
  `render_post_page` / `render_index_page` / `post_to_public_dict` as the
  live API, with `tags=[]` so the HTML matches `GET /{site}/{slug}` and
  `GET /{site}` byte-for-byte. Real tags are emitted only into
  `posts/{slug}/index.json` (JSON includes tags, mirroring nothing else).
- **Incremental**: per-post `render_hash` covers renderer version, site, base
  URL, post fields, tags, published/updated timestamps, and `content_hash`.
  Reuse requires the hash to match AND the files to exist on disk; with a
  `since` window, posts outside the window are always reused. Files dropped
  from the tree are pruned (with empty parent cleanup) using the previous
  manifest.
- **delta archive** (`since` set): only post files whose `updated_at` is
  inside the window plus `manifest.json`; site-level pages/feeds omitted, so a
  pull host merges just the changed files.
- **Leak guard**: base URL must be set (`site.base_url` or `--base-url`);
  loopback/private/link-local/reserved hosts are rejected (`422`). Verified by
  a test that scans every exported file for private-host substrings.
- **Deploy helper**: `plan_github_pages_deploy` returns a fixed command list
  (init branch, commit, force-push to `gh-pages`). `validate_repo` rejects
  malformed `owner/repo`.

## Honest limitations (not covered by automated tests)

1. **Real GitHub Pages push**: no credentials/CI secrets exist in this repo,
   so the workflow cannot be executed here (§"no CI secrets" constraint).
   `plan_github_pages_deploy` / CLI `--deploy --dry-run` are unit-tested for
   the command plan; the actual `git push -f origin gh-pages` is exercised
   only when a maintainer runs it with a token.
2. **Webhook-triggered rebuild**: the `repository_dispatch` trigger in
   `docs/ci/github-pages.yml` is written but not run end-to-end; wiring the
   AgentCMS webhook (#21) to GitHub's dispatch endpoint must be done by the
   platform owner (GitHub App or PAT + dispatch URL).
3. **Media**: `_collect_media` copies assets from a local `--media-root`
   (path = `media/{sha256}/{filename}`). There is no S3 client in the app
   dependencies, so remote/object-storage assets are skipped unless their
   bytes exist under the media root; the exported bundle otherwise self-heals
   because references are content-hashed.
4. **Postgres availability**: tests require an ephemeral PostgreSQL (the
   `db` fixture); they are skipped cleanly when none is available.

## Gate

`source .venv/bin/activate && python -m ruff format . && python -m pytest -q && python -m ruff check . && python -m ruff format --check . && python -m mypy`

Result at handoff: 682 passed, 1 skipped (docker), ruff + mypy clean.

## Hoisting notes for the next agent

- Incremental reuse requires the previous `manifest.json` in `--out`.
  Files are re-written on every run for determinism, but byte-identical.
- `since` on the CLI is compatible with `--incremental`: unchanged posts are
  reused, changed ones regenerated; files for posts outside the window are
  reused from the prior manifest (or regenerated as a safe fallback when the
  target dir is empty, e.g. the API's tempdir).
- The API builds the export into a `TemporaryDirectory` then archives from
  `manifest.json`; delta bundles therefore never include site-level files.
- `settings.default_site_slug` ("blog") is the CLI default when `--site` is
  omitted.