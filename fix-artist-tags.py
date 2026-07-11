#!/usr/bin/env python3
"""fix-artist-tags.py — Deja solo el artista PRINCIPAL en el tag ARTIST.

yt-dlp embebía todos los colaboradores unidos por coma ("A, B, C") y muchos
uploads traen además la lista completa de miembros/escritores. Poweramp y
Navidrome muestran esa string entera como un solo artista → lista de artistas
inservible.

Qué hace por archivo .opus cuyo ARTIST contiene coma / feat.:
  1. Calcula el principal: corta en " feat./ft./featuring " y luego en la
     primera ", " (con excepciones: whitelist de artistas con coma en el
     nombre, y sufijos "Jr."/"Sr.").
  2. Escribe ARTIST=principal y preserva el crédito completo en tag
     ARTIST_CREDIT (Vorbis custom, ningún player lo usa para agrupar).
     Mismo tratamiento para ALBUMARTIST si existe.
  3. Restaura el mtime original (los crons que filtran por -mtime no ven
     estos rewrites como descargas nuevas).
  4. Actualiza beets library.db (artist/artists/albumartist/albumartists)
     para que un `beet write` posterior NO revierta el cambio.

Uso:
    fix-artist-tags.py                # dry-run: solo reporta
    fix-artist-tags.py --apply        # aplica (backup de library.db antes)
    fix-artist-tags.py --report /tmp/reporte.tsv

Las descargas nuevas ya vienen limpias (dl-playlist.sh pasa
--parse-metadata "%(artists.0)s:%(meta_artist)s").
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

try:
    from mutagen.oggopus import OggOpus
except ImportError:
    print("❌ mutagen no disponible (usar el python del venv de scripts)",
          file=sys.stderr)
    sys.exit(2)

MUSIC_DIR = Path.home() / "storage/Music"
BEETS_DB = Path.home() / "scripts/beets/library.db"
# ␟ = separador que usa beets (>=2.0) para campos multi-valor
BEETS_SEP = "␟"

# Artistas reales que llevan coma en el nombre — NO partir.
WHITELIST = {
    "tyler, the creator",
    "crosby, stills & nash",
    "crosby, stills, nash & young",
    "earth, wind & fire",
    "emerson, lake & palmer",
    "blood, sweat & tears",
    "now, now",
    "grover washington, jr.",
}
SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii"}
FEAT_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
# " × " se usa a veces como separador de colaboración (Kabza De Small × ...)
X_SEP_RE = re.compile(r"\s+×\s+")


def primary_artist(full: str) -> str:
    """Artista principal de una string de crédito multi-artista."""
    base = FEAT_RE.split(full)[0].strip()
    if base.lower() in WHITELIST:
        return base
    base = X_SEP_RE.split(base)[0].strip()
    if ", " not in base:
        return base
    parts = [p.strip() for p in base.split(", ")]
    primary = parts[0]
    # "Grover Washington, Jr., Bill Withers" → mantener sufijo pegado
    if len(parts) > 1 and parts[1].lower() in SUFFIXES:
        primary = f"{parts[0]}, {parts[1]}"
    if primary.lower() in WHITELIST or f"{primary},".lower() in WHITELIST:
        return base
    return primary


def needs_fix(artist: str) -> bool:
    return bool(artist) and (", " in artist or FEAT_RE.search(artist)
                             or X_SEP_RE.search(artist))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Aplicar cambios (default: dry-run)")
    ap.add_argument("--music-dir", type=Path, default=MUSIC_DIR)
    ap.add_argument("--beets-db", type=Path, default=BEETS_DB)
    ap.add_argument("--report", type=Path, default=None,
                    help="TSV opcional: archivo, artista_viejo, artista_nuevo")
    args = ap.parse_args()

    changed, skipped, errors = [], 0, 0
    report_lines = ["archivo\tantes\tdespues"]

    con = None
    if args.apply and args.beets_db.exists():
        bak = args.beets_db.with_suffix(f".db.bak-{time.strftime('%Y%m%d')}")
        shutil.copy2(args.beets_db, bak)
        print(f"[beets] backup: {bak}")
        con = sqlite3.connect(args.beets_db)

    for opus in sorted(args.music_dir.glob("*.opus")):
        try:
            f = OggOpus(str(opus))
        except Exception:
            errors += 1
            continue
        artist = (f.get("artist") or [""])[0]
        if not needs_fix(artist):
            skipped += 1
            continue
        new = primary_artist(artist)
        if not new or new == artist:
            skipped += 1
            continue

        albumartist = (f.get("albumartist") or [""])[0]
        new_aa = primary_artist(albumartist) if needs_fix(albumartist) else None

        changed.append((opus.name, artist, new))
        report_lines.append(f"{opus.name}\t{artist}\t{new}")

        if args.apply:
            st = opus.stat()
            f["artist"] = [new]
            f["artist_credit"] = [artist]
            if new_aa:
                f["albumartist"] = [new_aa]
            f.save()
            os.utime(opus, (st.st_atime, st.st_mtime))
            if con is not None:
                con.execute(
                    """UPDATE items SET artist = ?, artists = ?,
                       albumartist = CASE WHEN ? != '' THEN ? ELSE albumartist END,
                       albumartists = CASE WHEN ? != '' THEN ? ELSE albumartists END
                       WHERE path = ?""",
                    (new, new, new_aa or "", new_aa or "",
                     new_aa or "", new_aa or "", str(opus.name).encode()))

    if con is not None:
        con.commit()
        con.close()

    if args.report:
        args.report.write_text("\n".join(report_lines) + "\n")
        print(f"[report] {args.report} ({len(changed)} cambios)")

    mode = "APLICADO" if args.apply else "DRY-RUN"
    print(f"[{mode}] cambiarían: {len(changed)} | sin cambio: {skipped} | "
          f"ilegibles: {errors}")
    for name, old, new in changed[:15]:
        print(f"  {new!r}  ⟵  {old[:70]!r}")
    if len(changed) > 15:
        print(f"  ... +{len(changed) - 15} más (ver --report)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
