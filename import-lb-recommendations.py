#!/usr/bin/env python3
"""import-lb-recommendations.py - Importa playlists recomendadas por ListenBrainz
y genera M3U Navidrome con los tracks que ya están en la biblioteca local.

Match por MusicBrainz Recording ID (MBID) — requiere que beets haya
re-tageado la library (musicbrainz plugin habilitado).

Uso:
    LB_USER=tuusuario import-lb-recommendations.py
    LB_USER=x LB_TOKEN=y import-lb-recommendations.py --match-fallback title-artist

Cron sugerido (diario 05:00):
    0 5 * * *   LB_USER=x /home/youruser/scripts/.venv/bin/python3 \\
        /home/youruser/scripts/import-lb-recommendations.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

LB_API = "https://api.listenbrainz.org/1"
DEFAULT_MUSIC_DIR = Path.home() / "storage/Music"
PLAYLISTS_DIR_DEFAULT = DEFAULT_MUSIC_DIR / "playlists"
RECOMMEND_TYPES = ["daily-jams", "weekly-jams", "weekly-exploration"]


def lb_get(endpoint: str, token: str | None = None) -> dict:
    req = urllib.request.Request(f"{LB_API}/{endpoint}")
    if token:
        req.add_header("Authorization", f"Token {token}")
    err = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            err = e
    raise err


def get_user_playlists(user: str, token: str | None = None) -> list[dict]:
    """Lista playlists creadas POR ListenBrainz para el user (Daily Jams etc.)."""
    data = lb_get(f"user/{user}/playlists/createdfor", token=token)
    return data.get("playlists", [])


def get_playlist_recordings(jspf_url: str) -> list[dict]:
    """Resuelve el JSPF de una playlist LB → lista de tracks con MBID."""
    # jspf_url típicamente termina en /playlist/<mbid> — endpoint = ese mismo path
    # /1/playlist/<mbid>
    m = re.search(r"/playlist/([a-f0-9-]+)", jspf_url)
    if not m:
        return []
    pid = m.group(1)
    data = lb_get(f"playlist/{pid}")
    tracks = data.get("playlist", {}).get("track", [])
    out = []
    for t in tracks:
        mbid = ""
        for ident in t.get("identifier", []) or []:
            mm = re.search(r"/recording/([a-f0-9-]+)", ident)
            if mm:
                mbid = mm.group(1); break
        out.append({
            "title": t.get("title", ""),
            "creator": t.get("creator", ""),
            "mbid": mbid,
        })
    return out


def load_local_index(beets_db: Path) -> dict[str, str]:
    """Carga mapping mb_trackid → ruta_archivo desde beets DB."""
    import sqlite3
    if not beets_db.exists():
        return {}
    conn = sqlite3.connect(beets_db)
    cur = conn.execute("SELECT mb_trackid, path FROM items WHERE mb_trackid != ''")
    out: dict[str, str] = {}
    for mbid, path_blob in cur:
        try:
            out[mbid] = path_blob.decode() if isinstance(path_blob, bytes) else str(path_blob)
        except Exception:
            continue
    conn.close()
    return out


def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s or "lb-mix"


def write_m3u(out: Path, name: str, paths: list[Path], music_dir: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for p in paths:
        # beets guarda paths RELATIVOS a su raíz (music_dir). Sin anclar a
        # music_dir, os.relpath los resolvía contra el CWD del cron (~/) →
        # entries rotas tipo "../../../track.opus". (Path absoluto se respeta:
        # music_dir / "/abs" == "/abs".)
        full = music_dir / p
        rel = os.path.relpath(full, out.parent)
        lines.append(rel)
    out.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("LB_USER", ""))
    ap.add_argument("--token", default=os.environ.get("LB_TOKEN", ""),
                    help="opcional para playlists privadas")
    ap.add_argument("--music-dir", type=Path, default=DEFAULT_MUSIC_DIR)
    ap.add_argument("--playlists-dir", type=Path, default=PLAYLISTS_DIR_DEFAULT)
    ap.add_argument("--beets-db", type=Path,
                    default=Path.home() / "scripts/beets/library.db")
    ap.add_argument("--missing-out", type=Path, default=None,
                    help="opcional: file con tracks LB que NO están local "
                         "(input para futuro auto-download)")
    args = ap.parse_args()

    if not args.user:
        print("❌ Falta --user (o env LB_USER)", file=sys.stderr)
        return 2

    print(f"🔍 Listando playlists ListenBrainz de '{args.user}'...")
    pls = get_user_playlists(args.user, args.token or None)
    if not pls:
        print("⚠️ Sin playlists createdfor — quizá necesitás scrobbles acumulados.")
        return 0

    print(f"📦 Cargando índice MBID→archivo desde beets ({args.beets_db})...")
    local = load_local_index(args.beets_db)
    print(f"   {len(local)} tracks con MBID en library local.")

    today = datetime.now().strftime("%Y-%m-%d")
    all_missing: list[dict] = []
    seen_kinds: set[str] = set()

    for pl in pls:
        title = pl.get("playlist", {}).get("title", "Untitled")
        identifier = pl.get("playlist", {}).get("identifier", "")
        kind = "?"
        for k in RECOMMEND_TYPES:
            if k in title.lower().replace(" ", "-"):
                kind = k; break

        # LB devuelve la edición actual Y la anterior de cada tipo (newest
        # first). Con filename estable, procesar la vieja pisaría a la nueva
        # → solo la primera de cada kind.
        if kind != "?" and kind in seen_kinds:
            print(f"\n⏭  {title} (kind={kind} ya procesado — edición vieja)")
            continue
        seen_kinds.add(kind)

        print(f"\n🎼 {title} (kind={kind})")
        recs = get_playlist_recordings(identifier)
        print(f"   {len(recs)} tracks en LB")

        local_paths: list[Path] = []
        missing_here: list[dict] = []
        for r in recs:
            mbid = r["mbid"]
            if mbid and mbid in local:
                local_paths.append(Path(local[mbid]))
            else:
                missing_here.append(r)

        # Filename Y display name ESTABLES por tipo (daily-jams / weekly-jams /
        # weekly-exploration): el título de LB incluye la semana ("... week of
        # 2026-07-07"), así que slugificarlo creaba un archivo NUEVO por semana
        # que nada limpiaba (cleanup excluye LB-*) → acumulación en Navidrome,
        # Syncthing y Poweramp. Con filename fijo cada corrida sobreescribe.
        # El header #PLAYLIST: también va fijo: Navidrome NO renombra la entidad
        # al cambiar el header (quedaba clavado el nombre del primer import), y
        # el nombre estable evita duplicados también en Poweramp.
        if kind != "?":
            display = "LB " + kind.replace("-", " ").title()
            out = args.playlists_dir / f"LB-{kind}.m3u"
        else:
            display = title
            out = args.playlists_dir / f"LB-{slugify(title)}.m3u"
        write_m3u(out, display, local_paths, args.music_dir)
        print(f"   → {out.name}: {len(local_paths)} local, {len(missing_here)} faltan")

        for r in missing_here:
            r["from_playlist"] = title
        all_missing.extend(missing_here)

    if args.missing_out and all_missing:
        args.missing_out.parent.mkdir(parents=True, exist_ok=True)
        args.missing_out.write_text(json.dumps(all_missing, indent=2))
        print(f"\n📝 {len(all_missing)} tracks faltantes → {args.missing_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
