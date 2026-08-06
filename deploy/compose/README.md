# deploy/compose — compose reference files

The authoritative compose files for the four tools live in their own repos
(`~/Projects/<tool>/compose.yaml` on cthulhu) and are used as-is by
`deploy.sh`. They are **not** duplicated here — only what repo-yard itself
owns goes in this directory.

| File | Purpose |
|---|---|
| `music-hub.compose.yaml` | Reference mirror of `music-hub/compose.yaml` (music-hub has no git repo; its live compose lives on cthulhu). |

## Fleet map (host ports on cthulhu, 10.8.0.10)

Traefik on singularity (10.8.0.1) routes `*.n0g.xyz` to these host ports.

| Service | Host port | Container port | Route |
|---|---|---|---|
| tidalwave | 19290 | 19290 | tw.n0g.xyz |
| spotifryer | 19293 | 19291 | sf.n0g.xyz |
| deeznutz | 19294 | 19290 | dz.n0g.xyz |
| qoochie | 19295 | 19290 | qz.n0g.xyz |
| music-hub | 19296 | 80 | (index page) |
| repo-yard | 19297 | 19297 | ry.n0g.xyz (search aggregator) |

Source of truth for this table: `deploy/services.conf` (the deploy script reads it).
