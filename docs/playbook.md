# Repo Yard — Ops Runbook

Reference for operating the quad + music-hub on cthulhu. The deploy workflow
itself lives in `docs/deploy.md`; this is the "something is wrong / what do I
need to know" document.

## Service map

| Service | Host port | Container port | Route | Mode |
|---|---|---|---|---|
| tidalwave | 19290 | 19290 | tw.n0g.xyz | git |
| spotifryer | 19293 | 19291 | sf.n0g.xyz | git |
| deeznutz | 19294 | 19290 | dz.n0g.xyz | git |
| qoochie | 19295 | 19290 | qz.n0g.xyz | git |
| music-hub | 19296 | 80 | (index page) | static |
| repo-yard | 19297 | 19297 | ry.n0g.xyz (unified search) | git |

Container names equal service names. Deploy dirs on cthulhu: `~/Projects/<service>`.

## Quick checks

```bash
ssh cthulhu "cd ~/Projects/repo-yard && ./deploy/status.sh"   # SHA + docker + HTTP per service
ssh cthulhu "docker ps --filter name=spotifryer --format '{{.Status}}'"   # single service health
ssh cthulhu "curl -s -o /dev/null -w '%{http_code}\n' http://10.8.0.10:19293/"   # raw route check (ports bind 10.8.0.10, not 127.0.0.1)
ssh cthulhu "docker logs --tail 100 <service>"                # app logs
tail -f ~/Projects/repo-yard/deploy/log/deploy.log            # deploy ledger
```

## Secrets & credentials

- **`.env` files** live only on cthulhu (`~/Projects/<tool>/.env`, gitignored).
  Contains `SECRET_KEY` (+ provider credentials). Never commit; a missing
  `SECRET_KEY` means random per restart → sessions/logins reset.
- **Platform tokens** (Spotify, Qobuz, Tidal, Deezer ARL) are persisted under
  `config/*/` inside each container and **auto-restored** by the tool's
  `entrypoint.sh` from `/opt/backups/<tool>-*` when missing:
  - spotifryer: `/opt/backups/spotifryer-config/` (streamrip `credentials.json` + `spotify_token.json`)
  - qoochie: `/opt/backups/qoochie-auth/qobuz_token.json.latest`
  - tidalwave: `/opt/backups/tidalwave-auth/token.json.latest` (NFS fallback: `/mnt/backups/cthulhu/tidalwave-auth/`)
  - deeznutz: `/opt/backups/deeznutz-auth/deezer_token.json.latest`
- If a token is lost and no backup exists, re-auth through the tool's web UI.

## Rollback

Automatic rollback happens on a failed health gate (`deploy.sh`). Manual:

```bash
ssh cthulhu
cd ~/Projects/<tool>
git checkout <known-good-sha>     # SHAs are in deploy/log/deploy.log
docker compose up -d              # add --build if requirements.txt/Dockerfile changed
# verify: curl the host port + status.sh
```

> After an automatic rollback the tool repo is left on a **detached HEAD**
> (rollback does `git checkout <sha>`). Re-attach before the next deploy:
> `git -C ~/Projects/<tool> checkout main`.

music-hub has no git: previous files are under
`~/Projects/repo-yard/deploy/log/music-hub.prev/` — restore and `compose up -d`.

## Debugging

- **Route returns 500**: the `error.html` template doesn't show the root cause —
  read `docker logs <service>` and look for a `Traceback` / exception line.
- **Health gate failed in deploy.sh**: check the same logs; the deploy is logged
  as `HEALTH GATE FAILED` with the from→to SHAs so you know exactly what shipped.
- **Token missing on restart**: `entrypoint.sh` warns — restore from
  `/opt/backups/<tool>-*` (see above) or re-auth.
- **Container won't start / crash-loops**: `docker logs <service>`, then
  `docker compose config` in `~/Projects/<service>` to validate the compose
  file, then `docker compose up -d --build` only as an emergency (the deploy
  script is the normal path).
- **Port conflict**: host ports are fixed (see service map); changing them means
  updating `deploy/services.conf` **and** the Traefik route on singularity.

## Conventions

- Deploy via `deploy/deploy.sh`, never bare `docker compose up -d --build`.
- The tool dirs are symlinks to the live repos — tool code is edited in the
  live repo (`Projects/<tool>`) and shows up here automatically; commit in
  repo-yard only when `app/`, `deploy/`, `docs/`, or `music-hub/` changed.
- `deploy/log/` is the one place deploy state accumulates — it is gitignored.
