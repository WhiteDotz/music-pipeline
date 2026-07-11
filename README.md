# music-pipeline

Self-hosted music automation: a set of bash/Python tools that download music,
enrich its metadata, generate rotating playlists and keep a
[Navidrome](https://www.navidrome.org/) library healthy — orchestrated
entirely by cron on a headless Debian home server, synced to a phone with
Syncthing.

Sanitized snapshot of a personal production pipeline (running daily for
months). Credentials are read from environment files that are **not** in this
repo; hosts and contact addresses were replaced with placeholders.

## Pipeline

```
yt-dlp download        beets + MusicBrainz        M3U generation        Navidrome
dl-playlist.sh   ──►   beets-enrich.sh      ──►   playlist-m3u.py  ──►  (scan API)
dl-single.sh                                                             │
                                                                         ▼
                                                              phone (Syncthing)
```

### Ingest

- **`dl-playlist.sh`** — downloads a playlist/URL list as opus with correct
  tags (`--parse-metadata` so ARTIST holds only the main artist; full credit
  preserved in `ARTIST_CREDIT`), dedup by download archive, fullwidth-safe
  filenames for Android storage.
- **`dl-single.sh`** — single-track variant used by the web request app.
- **`beets-enrich.sh`** — re-tags new files against MusicBrainz via beets.

### Playlist generators (cron)

- **`playlist-m3u.py`** — turns ID lists into M3U files with `#PLAYLIST:`
  display names (Navidrome tracks imported M3Us by path and renames the
  entity when the header changes — exploited to keep stable filenames).
- **`my-music-m3u.py`** — consolidated "liked" playlist: Navidrome stars ∪
  ListenBrainz loves ∪ a frozen likes snapshot.
- **`sync-lb-stars.py`** — mirrors ListenBrainz loves into Navidrome stars
  (Subsonic API, salted-token auth).
- **`import-lb-recommendations.py`** — imports ListenBrainz weekly
  recommendation playlists, matching recordings to local files.
- **`personal-mixes.py`** — asks an LLM (Gemini CLI) to design themed mixes
  from library candidates, validates artists against MusicBrainz.
- **`weekly-axis.sh`** + **`music-axes.txt`** — deterministic weekly rotation
  of discovery themes (ISO week number modulo list length).
- **`weekly-downloads.sh`** — "last 7 days" playlist.
- **`cleanup-temp-playlists.py`** — retention policy: discovery playlists
  archive their files after 60 days, personal mixes rotate after 21 days
  (playlist deleted, files untouched); protected keep-list; deletes via the
  Subsonic API so clients drop them too.

### Library maintenance

- **`fix-artist-tags.py`** — retroactive ARTIST tag fix across the library,
  updating the beets DB so `beet write` doesn't revert it; whitelist for
  names containing commas ("Tyler, The Creator").
- **`merge-artist-variants.py`** — unifies artist-name variants Navidrome
  splits (case/apostrophes automatically, curated map for collabs/suffixes);
  dry-run by default, `--apply` backs up the beets DB first.
- **`dedup-music.py`** — classifies duplicate pairs by audio hash
  (dupe / different / version / orphan) into a TSV report.
- **`dedup-resolve.py`** — consumes the report and does the full
  accounting: trash with tombstones, videoId redirects, M3U rewrite + dedup,
  beets DB row removal with backup.

### Web request app

- **`music-request/app.py`** — small Flask app: search (ytmusicapi) or paste
  a URL, one-click download through the full pipeline, "already in library"
  badge (download archive ∪ normalized title|artist match against the beets
  DB), single-worker job queue.

## Conventions

- Credentials only via environment (`~/.lb-sync.env`, mode 600) — see
  `.lb-sync.env.example`. Nothing secret in code or crontab.
- Every cron job logs to a central log dir with self-managed rotation.
- Destructive tools default to dry-run and back up databases before writing.
