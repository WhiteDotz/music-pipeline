#!/usr/bin/env python3
"""my-music-m3u.py — Playlist consolidada "My Music".

Unión de todas las fuentes de "me gusta":
  1. Starred en Navidrome (corazón en Feishin, y loves de ListenBrainz que
     sync-lb-stars.py convierte en stars — Pano Scrobbler en el celular).
  2. Likes de YouTube Music: entradas de liked-music.m3u (generado por
     dl-playlist.sh al bajar la playlist "Liked Music").

Salida: ~/storage/Music/playlists/my-music.m3u con header "#PLAYLIST:My Music".
Filename e identidad ESTABLES → Navidrome/Poweramp actualizan en vez de
duplicar. "My Music" está en la keep-list de cleanup-temp-playlists.py, así que
sus tracks nunca se archivan.

Lee navidrome.db directo (read-only), sin credenciales.

Cron sugerido (tras sync-lb-stars de las 04:30):
    45 4 * * * /usr/bin/python3 /home/youruser/scripts/my-music-m3u.py
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_MUSIC_DIR = Path.home() / "storage/Music"
DEFAULT_NAVIDROME_DB = Path.home() / "docker/navidrome/data/navidrome.db"
PLAYLIST_NAME = "My Music"


def starred_paths(db: Path) -> list[str]:
    """Paths (relativos a la library) de tracks starred, más reciente primero."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute("""
        SELECT mf.path
        FROM media_file mf
        JOIN annotation a
          ON a.item_id = mf.id AND a.item_type = 'media_file'
        WHERE a.starred = 1
          AND mf.path NOT LIKE '.trash%'
        ORDER BY a.starred_at DESC
    """).fetchall()
    con.close()
    return [r[0] for r in rows]


def liked_entries(liked_m3u: Path) -> list[str]:
    """Entradas del M3U de Liked Music, ya relativas a playlists/ (../x.opus)."""
    if not liked_m3u.exists():
        return []
    return [l for l in liked_m3u.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-dir", type=Path, default=DEFAULT_MUSIC_DIR)
    ap.add_argument("--navidrome-db", type=Path, default=DEFAULT_NAVIDROME_DB)
    ap.add_argument("--out", type=Path, default=None,
                    help="Default: <music-dir>/playlists/my-music.m3u")
    args = ap.parse_args()

    music_dir = args.music_dir.expanduser()
    playlists_dir = music_dir / "playlists"
    out = args.out or (playlists_dir / "my-music.m3u")
    out.parent.mkdir(parents=True, exist_ok=True)

    if not args.navidrome_db.exists():
        print(f"❌ navidrome.db no existe: {args.navidrome_db}", file=sys.stderr)
        return 2

    # Starred primero (lo más "curado"), luego likes YT que no estén ya.
    entries: list[str] = []
    seen: set[str] = set()
    missing = 0
    for path in starred_paths(args.navidrome_db):
        rel = os.path.relpath(music_dir / path, out.parent)
        if rel in seen:
            continue
        if not (music_dir / path).exists():
            missing += 1
            continue
        entries.append(rel)
        seen.add(rel)

    n_starred = len(entries)
    for rel in liked_entries(playlists_dir / "liked-music.m3u"):
        if rel in seen:
            continue
        if not (out.parent / rel).exists():
            missing += 1
            continue
        entries.append(rel)
        seen.add(rel)

    lines = ["#EXTM3U", f"#PLAYLIST:{PLAYLIST_NAME}"] + entries
    out.write_text("\n".join(lines) + "\n")
    print(f"[my-music] {out.name}: {len(entries)} tracks "
          f"({n_starred} starred + {len(entries) - n_starred} liked-YT nuevos, "
          f"{missing} sin archivo local)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
