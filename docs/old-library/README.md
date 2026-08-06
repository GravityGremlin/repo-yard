# Old Library — Redownload Manifests

Slimmed from the full 11,566-artist inventory (`docs/old-library-artists.md`).
Old library: `/mnt/hive/library/music` (retained on disk; eventual redownload target).
Manifests generated 2026-08-05. "in_active" = artist already present in the active
`music_morpheus` beets library (still may have missing albums/tracks — verify per artist).

## Strategy buckets

| Bucket | File | Artists | Tracks | Bytes | Share of tracks |
|---|---|---|---|---|---|
| **Phase 1 — core** (>=10 tracks, clean) | `phase-1-core.csv` | 1777 | 135,243 | 620.5GB | 85.8% |
| **Phase 2 — tail** (3-9 tracks, clean) | `phase-2-tail.csv` | 1935 | 9,691 | 44.2GB | 6.1% |
| **Review** (compilations/OST/audiobook/high-ratio) | `anomalies.csv` | 28 | 3,533 | 11.8GB | 2.2% |
| **Deferred** (1-2 tracks) | `deferred-tiny.csv` | 7811 | 9,146 | 31.7GB | 5.8% |
| **Junk** (empty dirs) | `empty-dirs.csv` | 15 | 0 | 0 | 0% |

**Totals:** 11566 artists · 157,613 tracks · 708.3GB

## Recommended order

1. **Phase 1** — start with the top 200 by track count (~46% of all content),
   then work down. Each row carries `in_active` so already-covered artists can be
   skipped or spot-verified.
2. **Phase 2** — after phase 1, for completeness (6.1% of content).
3. **Anomalies** — per-case decision: skip audiobooks (e.g. Susanna Clarke),
   decide OSTs/game soundtracks, dedupe "Various Artists".
4. **Deferred tiny** — mostly singles/EPs; revisit only if total completeness matters.

## Notes

- Anomaly heuristic: name matches various/compilation/soundtrack/OST/audiobook/
  motion-picture, OR tracks-per-album > 40 (audiobooks, grindcore mega-EPs).
  Real artists with many live/compilation albums (Queen, Ramones, Leadbelly)
  stay in phase-1 — only genuine non-music entries are flagged.
- The full per-artist album/track/byte inventory remains in
  `/mnt/backups/library-stock/2026-08-05/artists.csv`.
