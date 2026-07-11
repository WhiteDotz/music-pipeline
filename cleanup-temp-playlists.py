#!/usr/bin/env python3
"""cleanup-temp-playlists.py - Rota playlists temporales y archiva sus canciones.

Workflow:
1. Lista playlists vía Subsonic API.
2. Marca como TEMPORAL toda playlist cuyo nombre matchee alguno de los
   patrones regex (default archive: "^W\\d+:" | "^Semana-"; rotate:
   "^Mix:" | "^personal-mix-") y tenga >=N días de edad.
3. Construye el set de canciones PROTEGIDAS:
     - starred (getStarred2), y
     - cualquier track dentro de una playlist PERMANENTE por nombre
       (default: "Liked Music", "Weekly Downloads (last 7d)").
   Tu biblioteca base vive en "Liked Music" → queda protegida aunque un mix
   temporal la haya tocado. Solo los downloads de temporada (no en Liked Music,
   no starred) se archivan.
4. Por cada playlist temporal vieja:
     - archiva a ARCHIVE_DIR los .opus NO protegidos (salen de ~/storage/Music
       → Syncthing borra la copia del celular),
     - BORRA la entidad playlist en Navidrome (deletePlaylist) → desaparece de
       Feishin,
     - borra el M3U correspondiente en playlists-dir → Syncthing lo quita del cel.
5. Re-escanea Navidrome (POST /rest/startScan).

Uso:
    cleanup-temp-playlists.py --user admin --pass MIPASS
    cleanup-temp-playlists.py --dry-run  # simular sin mover/borrar nada
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "http://localhost:4533"
# Credenciales: mismo esquema que sync-lb-stars.py — se leen de un .env
# (KEY=VALUE) para no llevar el password en texto plano en la crontab.
ENV_CANDIDATES = [
    Path.home() / ".lb-sync.env",
    Path.home() / ".apis.env",
]


def load_env() -> None:
    for env_file in ENV_CANDIDATES:
        if not env_file.exists():
            continue
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return
# Dos clases de playlist temporal:
#   ARCHIVE = trae downloads nuevos (descubrimiento). Al vencer: archiva los
#             .opus no protegidos + borra playlist + M3U.
#   ROTATE  = solo re-baraja biblioteca existente. Al vencer: borra playlist +
#             M3U, pero NUNCA toca archivos (no agregó nada nuevo).
# Patrones REGEX (no prefijos simples) sobre el nombre display de la playlist.
# Incluyen el esquema nuevo ("W24: Bossa Nova", "Mix: Título") y el viejo
# ("Semana-*", "personal-mix-*") para gestionar playlists en transición.
# OJO: "^W\d+:" exige dígito tras la W → NO matchea "Weekly Downloads (last 7d)".
DEFAULT_ARCHIVE_PREFIXES = [r"^W\d+:", r"^Semana-"]
DEFAULT_ROTATE_PREFIXES = [r"^Mix:", r"^personal-mix-"]
DEFAULT_KEEP_NAMES = ["Liked Music", "Weekly Downloads (last 7d)", "My Music",
                      "Pedidos"]
# Edad distinta por clase: el descubrimiento (Semana-*) vive 2 meses antes de
# archivar sus canciones; la rotación de mixes (personal-mix-*) es declutter de
# Feishin → más corta para no volver a ahogarse.
DEFAULT_ARCHIVE_AGE_DAYS = 60
DEFAULT_ROTATE_AGE_DAYS = 21
DEFAULT_MUSIC_DIR = Path.home() / "storage/Music"
DEFAULT_ARCHIVE_DIR = Path.home() / "storage/Music_archive"
DEFAULT_PLAYLISTS_DIR = Path.home() / "storage/Music/playlists"


def subsonic_call(base: str, user: str, password: str, endpoint: str,
                  params: dict | None = None) -> dict:
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode()).hexdigest()
    q = {
        "u": user, "t": token, "s": salt,
        "v": "1.16.1", "c": "cleanup-temp-playlists", "f": "json",
    }
    if params:
        q.update(params)
    url = f"{base.rstrip('/')}/rest/{endpoint}?{urllib.parse.urlencode(q, doseq=True)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.loads(r.read().decode())
    sub = data.get("subsonic-response", {})
    if sub.get("status") != "ok":
        raise RuntimeError(f"{endpoint} → {sub.get('error', sub)}")
    return sub


def list_playlists(base, user, pw):
    r = subsonic_call(base, user, pw, "getPlaylists")
    return r.get("playlists", {}).get("playlist", [])


def get_playlist(base, user, pw, pid):
    r = subsonic_call(base, user, pw, "getPlaylist", {"id": pid})
    return r.get("playlist", {})


def get_starred(base, user, pw):
    r = subsonic_call(base, user, pw, "getStarred2")
    songs = r.get("starred2", {}).get("song", [])
    return {s["id"] for s in songs}


def delete_playlist(base, user, pw, pid):
    subsonic_call(base, user, pw, "deletePlaylist", {"id": pid})


def trigger_scan(base, user, pw):
    try:
        subsonic_call(base, user, pw, "startScan")
    except Exception as e:
        print(f"[warn] no pude triggerear scan: {e}", file=sys.stderr)


def parse_date(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def matches_prefix(name: str, patterns: list[str]) -> bool:
    """True si el nombre matchea ALGÚN patrón regex (anclados al inicio)."""
    return any(re.search(p, name) for p in patterns)


def safe_exists(p: Path) -> bool:
    """exists() que devuelve False ante paths inválidos (ENAMETOOLONG, etc.)
    en vez de lanzar — algunos tracks traen artistas larguísimos en el path."""
    try:
        return p.exists()
    except OSError:
        return False


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("NAVIDROME_BASE", DEFAULT_BASE))
    ap.add_argument("--user", default=os.environ.get("NAVIDROME_USER", "admin"))
    ap.add_argument("--password", "--pass", dest="password",
                    default=os.environ.get("NAVIDROME_PASS", ""))
    ap.add_argument("--archive-prefix", default=",".join(DEFAULT_ARCHIVE_PREFIXES),
                    help="Regex (coma) cuyo match archiva archivos al vencer")
    ap.add_argument("--rotate-prefix", default=",".join(DEFAULT_ROTATE_PREFIXES),
                    help="Regex (coma) cuyo match solo borra playlist+M3U al vencer")
    ap.add_argument("--keep", default=",".join(DEFAULT_KEEP_NAMES),
                    help="Nombres de playlists que PROTEGEN sus tracks (coma)")
    ap.add_argument("--archive-age-days", type=int, default=DEFAULT_ARCHIVE_AGE_DAYS,
                    help="Días antes de archivar canciones de prefijos archive")
    ap.add_argument("--rotate-age-days", type=int, default=DEFAULT_ROTATE_AGE_DAYS,
                    help="Días antes de rotar playlists de prefijos rotate")
    ap.add_argument("--music-dir", type=Path, default=DEFAULT_MUSIC_DIR)
    ap.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    ap.add_argument("--playlists-dir", type=Path, default=DEFAULT_PLAYLISTS_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.password:
        print("❌ Falta password (--password o env NAVIDROME_PASS)", file=sys.stderr)
        return 2

    archive_prefixes = [p.strip() for p in args.archive_prefix.split(",") if p.strip()]
    rotate_prefixes = [p.strip() for p in args.rotate_prefix.split(",") if p.strip()]
    keep_names = {n.strip() for n in args.keep.split(",") if n.strip()}

    args.archive_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    print(f"🔍 Listando playlists en {args.base}...")
    print(f"   ARCHIVAN: {archive_prefixes} (>= {args.archive_age_days}d)")
    print(f"   solo ROTAN: {rotate_prefixes} (>= {args.rotate_age_days}d)")
    print(f"   Playlists protectoras: {sorted(keep_names)}")
    playlists = list_playlists(args.base, args.user, args.password)
    temp = []  # (playlist, age, mode) con mode in {"archive","rotate"}
    keep_playlists = []
    for p in playlists:
        name = p.get("name", "")
        mode = age_limit = None
        if matches_prefix(name, archive_prefixes):
            mode, age_limit = "archive", args.archive_age_days
        elif matches_prefix(name, rotate_prefixes):
            mode, age_limit = "rotate", args.rotate_age_days
        if mode:
            age = (now - parse_date(p["created"])).days
            if age >= age_limit:
                temp.append((p, age, mode))
        elif name in keep_names:
            keep_playlists.append(p)

    if not temp:
        print("✅ Ninguna playlist temporal vencida")
        return 0

    n_arch = sum(1 for _, _, m in temp if m == "archive")
    n_rot = len(temp) - n_arch
    print(f"📦 {len(temp)} playlist(s) vieja(s): {n_arch} archivan, {n_rot} solo rotan; "
          f"{len(keep_playlists)} protectora(s).")

    starred_ids = get_starred(args.base, args.user, args.password)
    print(f"⭐ {len(starred_ids)} canciones starred (protegidas).")

    # Tracks protegidos por estar en una playlist permanente (Liked Music, etc.)
    keep_song_ids: set[str] = set(starred_ids)
    for p in keep_playlists:
        det = get_playlist(args.base, args.user, args.password, p["id"])
        for s in det.get("entry", []):
            keep_song_ids.add(s["id"])
    print(f"🔒 {len(keep_song_ids)} canciones protegidas en total "
          f"(starred + playlists permanentes).")

    moved = []
    skipped_keep = 0
    deleted_playlists: list[str] = []
    temp_names: list[str] = []
    for p, age, mode in temp:
        det = get_playlist(args.base, args.user, args.password, p["id"])
        entries = det.get("entry", [])
        tag = "archiva" if mode == "archive" else "solo rota"
        print(f"\n🗓  '{p['name']}' ({age}d, {len(entries)} tracks) [{tag}]")
        temp_names.append(p["name"])
        # Solo el modo "archive" (Semana-*) mueve archivos. "rotate"
        # (personal-mix-*) deja la biblioteca intacta.
        if mode == "archive":
            for s in entries:
                sid, path = s["id"], s.get("path", "")
                if sid in keep_song_ids:
                    skipped_keep += 1
                    continue
                full = args.music_dir / path
                if not safe_exists(full):
                    # Fallback: storage es flat, intentar solo basename
                    full = args.music_dir / Path(path).name
                if not safe_exists(full):
                    continue  # ya archivado en corrida previa
                dest = args.archive_dir / full.name
                print(f"  → archive: {full.name}")
                if not args.dry_run:
                    shutil.move(str(full), str(dest))
                moved.append(full.name)

        # Borrar la entidad playlist en Navidrome (clave: la quita de Feishin)
        if args.dry_run:
            print(f"  (dry-run) deletePlaylist '{p['name']}' (id={p['id']})")
        else:
            try:
                delete_playlist(args.base, args.user, args.password, p["id"])
                print(f"  🗑  playlist borrada en Navidrome (id={p['id']})")
            except Exception as e:
                print(f"  ⚠️ no pude borrar playlist {p['id']}: {e}")
        deleted_playlists.append(p["name"])

    print(f"\n📊 archivados: {len(moved)} | protegidos: {skipped_keep} | "
          f"playlists borradas: {len(deleted_playlists)}")

    if args.dry_run:
        print("(dry-run — sin cambios reales)")
        return 0

    # Borrar el M3U de cada playlist temporal procesada (incondicional: ya
    # borramos la entidad en Navidrome). Syncthing propaga el borrado al cel.
    removed_m3u = remove_temp_m3u(args.playlists_dir, set(temp_names))
    if removed_m3u:
        print(f"🗑  M3U borrados: {len(removed_m3u)}")
        for n in removed_m3u:
            print(f"   - {n}")

    print("🔄 Triggereando scan Navidrome...")
    trigger_scan(args.base, args.user, args.password)
    return 0


def remove_temp_m3u(playlists_dir: Path, temp_names: set[str]) -> list[str]:
    """Borra todo M3U cuyo header #PLAYLIST: matchee una playlist temporal
    procesada. Incondicional: la entidad ya se borró en Navidrome."""
    if not playlists_dir.is_dir():
        return []
    removed: list[str] = []
    for m3u in playlists_dir.glob("*.m3u"):
        try:
            lines = m3u.read_text().splitlines()
        except OSError:
            continue
        header = next((l[len("#PLAYLIST:"):].strip() for l in lines
                       if l.startswith("#PLAYLIST:")), None)
        if header in temp_names:
            m3u.unlink()
            removed.append(m3u.name)
    return removed


if __name__ == "__main__":
    sys.exit(main())
