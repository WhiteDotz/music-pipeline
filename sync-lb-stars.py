#!/usr/bin/env python3
"""sync-lb-stars.py — Sincroniza loves de ListenBrainz y plays >= N a stars en Navidrome.

Dos triggers para star automático:
  1. Track marcado como "love" en ListenBrainz (vía Pano Scrobbler en celular,
     o cualquier cliente LB).
  2. Track con play_count >= STAR_THRESHOLD plays (por defecto 10) en Navidrome.

Match LB → Navidrome vía MusicBrainz Recording ID (mbz_recording_id).
Star vía Subsonic API /rest/star.

Cron sugerido: 0 4 * * *

Config: lee env vars desde ~/.apis.env o ~/.lb-sync.env si existe.
  LB_USER             ListenBrainz username (público, suficiente para read)
  NAVIDROME_BASE      URL Navidrome (default http://localhost:4533)
  NAVIDROME_USER      Admin user (default 'admin')
  NAVIDROME_PASS      Admin password
  NAVIDROME_DB        Path a navidrome.db (default ~/docker/navidrome/data/navidrome.db)
  STAR_THRESHOLD      Plays mínimos para auto-star (default 10)
  LB_API              Base API LB (default https://api.listenbrainz.org)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENV_CANDIDATES = [
    Path.home() / ".lb-sync.env",
    Path.home() / ".apis.env",
]


def load_env() -> None:
    """Carga primer .env existente en os.environ (formato KEY=VALUE)."""
    for env_file in ENV_CANDIDATES:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)
        return


def required(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"ERROR: variable {key} no definida en env", file=sys.stderr)
        sys.exit(2)
    return val


def lb_get_loves(api: str, user: str) -> set[str]:
    """Devuelve set de recording MBIDs marcados como love (score=1)."""
    url = f"{api}/1/feedback/user/{urllib.parse.quote(user)}/get-feedback?score=1&count=1000"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"ERROR LB API HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(3)

    mbids: set[str] = set()
    for fb in data.get("feedback", []):
        mbid = fb.get("recording_mbid")
        if mbid:
            mbids.add(mbid)
    print(f"[lb] {len(mbids)} loves recuperadas para usuario {user}", file=sys.stderr)
    return mbids


def navidrome_candidates(db_path: Path, threshold: int,
                        love_mbids: set[str]) -> list[tuple[str, str, str, int]]:
    """Devuelve tracks a starrear: por play_count >= threshold o por MBID en loves.

    Excluye los ya starred.
    Retorna list de (track_id, title, artist, reason_code) donde reason ∈ {plays, love}.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.cursor()

    # Tracks con plays >= threshold no-starred
    cur.execute("""
        SELECT mf.id, mf.title, mf.artist, a.play_count
        FROM media_file mf
        LEFT JOIN annotation a
          ON a.item_id = mf.id AND a.item_type = 'media_file'
        WHERE COALESCE(a.play_count, 0) >= ?
          AND COALESCE(a.starred, 0) = 0
    """, (threshold,))
    by_plays = cur.fetchall()

    # Tracks con MBID en love_mbids no-starred
    by_love: list[tuple[str, str, str, int]] = []
    if love_mbids:
        placeholders = ",".join("?" * len(love_mbids))
        cur.execute(f"""
            SELECT mf.id, mf.title, mf.artist, COALESCE(a.play_count, 0)
            FROM media_file mf
            LEFT JOIN annotation a
              ON a.item_id = mf.id AND a.item_type = 'media_file'
            WHERE mf.mbz_recording_id IN ({placeholders})
              AND COALESCE(a.starred, 0) = 0
        """, tuple(love_mbids))
        by_love = cur.fetchall()

    con.close()

    # Merge — un track puede triggerear por ambos motivos; gana love en el log
    seen: dict[str, tuple[str, str, str, str]] = {}
    for tid, title, artist, pc in by_plays:
        seen[tid] = (tid, title, artist, f"plays={pc}")
    for tid, title, artist, pc in by_love:
        seen[tid] = (tid, title, artist, "love" if tid not in seen else f"love+plays={pc}")

    out = [(tid, title, artist, reason) for tid, (_, title, artist, reason) in seen.items()]
    print(f"[nd] candidatos a star: {len(out)} "
          f"(plays>={threshold}: {len(by_plays)}, loves: {len(by_love)})",
          file=sys.stderr)
    return out


def subsonic_star(base: str, user: str, password: str, track_id: str) -> bool:
    """Marca star vía Subsonic API. Retorna True si OK."""
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    params = {
        "u": user, "t": token, "s": salt,
        "v": "1.16.1", "c": "sync-lb-stars", "f": "json",
        "id": track_id,
    }
    url = f"{base.rstrip('/')}/rest/star?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  [ERR] HTTP {e.code} star {track_id}", file=sys.stderr)
        return False
    status = data.get("subsonic-response", {}).get("status")
    return status == "ok"


def main() -> int:
    load_env()
    lb_user = required("LB_USER")
    nd_base = os.environ.get("NAVIDROME_BASE", "http://localhost:4533")
    nd_user = os.environ.get("NAVIDROME_USER", "admin")
    nd_pass = required("NAVIDROME_PASS")
    nd_db = Path(os.environ.get("NAVIDROME_DB",
                                str(Path.home() / "docker/navidrome/data/navidrome.db")))
    threshold = int(os.environ.get("STAR_THRESHOLD", "10"))
    lb_api = os.environ.get("LB_API", "https://api.listenbrainz.org")

    if not nd_db.exists():
        print(f"ERROR: Navidrome DB no encontrada en {nd_db}", file=sys.stderr)
        return 2

    love_mbids = lb_get_loves(lb_api, lb_user)
    candidates = navidrome_candidates(nd_db, threshold, love_mbids)

    if not candidates:
        print("[done] nada que starrear hoy", file=sys.stderr)
        return 0

    ok = 0
    fail = 0
    for tid, title, artist, reason in candidates:
        if subsonic_star(nd_base, nd_user, nd_pass, tid):
            print(f"  [STAR] {artist} — {title}  ({reason})", file=sys.stderr)
            ok += 1
        else:
            fail += 1

    print(f"[done] starred={ok} fallidos={fail}", file=sys.stderr)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
