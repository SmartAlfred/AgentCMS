# Backup / restore drill — 2026-09-25 (measured RPO 13 s, RTO 54 s)

Stack: `docker compose -p ws3-drill -f deploy/compose/docker-compose.prod.yml` (production-shaped: db + one-shot
migrate + api + caddy), image `agentcms:v0.3.0`, PostgreSQL 16, MinIO as the off-site S3. Host: worker (macOS arm64),
client `pg_dump` 18.3. All timestamps UTC, taken with `date -u +%FT%TZ` in the shell that ran the command.
Isolation: throwaway compose project, ports remapped (api `127.0.0.1:8033`, db `127.0.0.1:55433`, MinIO `127.0.0.1:59000`).

## 1. Seed real content through the API

```
T_pre_write     2026-09-25T12:09:09Z  POST /c/<cap_blog_…>/posts  {"slug":"pre-backup-marker"} -> 201 id 48b44325-d61a-48b4-8aab-bdec0ed65f90
T_pre_published 2026-09-25T12:09:13Z  POST /c/<cap_blog_…>/posts/48b44325-…/publish -> 200
                                      GET /v1/posts/pre-backup-marker (Bearer cap_…) -> 200 ; GET /blog/pre-backup-marker -> 200
```

## 2. Backup — two defects found before it would work

`DATABASE_URL=<host dsn> S3_ENDPOINT_URL=http://127.0.0.1:59000 python -m scripts.backup`

```
12:09:18Z  403 SignatureDoesNotMatch   PUT http://127.0.0.1:59000/20260925-120918.dump.enc     <- signed for <bucket>.s3.us-east-1.amazonaws.com
12:11:07Z  403 SignatureDoesNotMatch   PUT …/agentcms-media/20260925-121107.dump.enc          <- raw "/" and ";" in the canonical query
```

Three defects on the off-site path, all fixed and pushed with regression tests (2 of 5 new cases fail on the
unpatched files):

| commit | fix |
| --- | --- |
| `43e9e50` | sign the host actually contacted; path-style addressing for self-hosted endpoints |
| `1b0c758` | URI-encode the SigV4 canonical query string (`X-Amz-Credential`'s `/`, `X-Amz-SignedHeaders`' `;`) |
| `1b0c758` | presign against the bucket in `BACKUP_STORE_URL` instead of the media bucket |

```
T_backup_start  2026-09-25T12:12:10Z  python -m scripts.backup
T_backup_done   2026-09-25T12:12:11Z  backup ok: 20260925-121211.dump.enc (0.2s)  exit 0
                                      local  sha256 d00d6a2e8802d67cc436494e1ac9c7c4d1a575d472c6f6244bf8d38648e18935  (71,330 bytes)
                                      offsite sha256 d00d6a2e8802d67cc436494e1ac9c7c4d1a575d472c6f6244bf8d38648e18935  MATCH
```

## 3. The failure: volume destroyed, local copies gone

```
T_wrote         2026-09-25T12:12:23Z  POST /c/<cap_blog_…>/posts {"slug":"post-backup-marker"} -> 201 draft 8ffb882a-5c7a-4b49-8961-6baf7b792b90
T_loss          2026-09-25T12:12:24Z  docker compose -p ws3-drill rm -sf db ; docker volume rm -f ws3-drill_pgdata-prod
                                      volumes left: ws3-drill_caddy-* , ws3-drill_drill-miniodata   (the off-site store is a separate failure domain)
                                      GET /readyz -> 503 {"code":"database-unavailable"}            <- degraded semantics behave
                                      mv .backups /tmp/ws3-backups-lost-121224                        <- only the off-site copy now exists
```

## 4. Restore from the off-site copy only

```
T_offsite_fetch 2026-09-25T12:12:32Z  fget_object s3://agentcms-backups/20260925-121211.dump.enc -> sha256 d00d6a2e… MATCH
12:12:33Z                             docker compose up -d db (fresh volume)
12:12:33Z                             first restore attempt: exit 1 "server closed the connection unexpectedly"
                                      (Postgres was still inside its first-boot init restart; pg_isready had already passed)
T_restore       2026-09-25T12:13:13Z  psql -c 'drop schema public cascade; create schema public;'
T_restore_done  2026-09-25T12:13:14Z  python -m scripts.restore --dump /tmp/ws3-restore/20260925-121211.dump.enc -> restore ok (1.1 s, 18 tables)
```

## 5. Verify through the API, not through a database ping

```
T_serve         2026-09-25T12:13:18Z  GET /v1/posts/pre-backup-marker -> 200  id 48b44325-…  status published  body matches
                                      GET /blog/pre-backup-marker -> 200           (public page renders restored content)
                                      GET /readyz -> 200
                                      GET /v1/posts/post-backup-marker -> 404      <- the 12:12:23Z write is gone, as predicted
```

## Numbers

* **RPO = 13 s** (T_loss 12:12:24Z − T_backup 12:12:11Z). Proof: the object created 12 s after the backup is absent
  after the restore; everything before it is present.
* **RTO = 54 s** (T_serve 12:13:18Z − T_loss 12:12:24Z), broken down as: off-site fetch 8 s | fresh Postgres first boot
  9 s | failed first attempt + wait for the init restart 39 s | clean restore 1.1 s | API serving 4 s.
  A clean run that waits for Postgres to finish initialising skips that 39 s, i.e. ~15 s to serving.
* Unhappy paths: wrong passphrase → exit 1; truncated dump → exit 1 (detected at decrypt); missing dump → exit 2.
  The live database was untouched by all three (4 posts afterwards).
