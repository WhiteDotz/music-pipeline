#!/usr/bin/env python3
"""merge-artist-variants.py — Unifica variantes del mismo artista que
Navidrome/Poweramp muestran como artistas separados.

Dos fuentes de variantes:
  1. AUTO: mismo nombre normalizado (casefold + solo caracteres de palabra),
     distinto raw — "Korn"/"KoRn", "L'Impératrice"/"L’Impératrice",
     "Caravan Palace"/"CaravanPalace". Canónico = variante con más archivos;
     empate → apóstrofe tipográfico (’) primero, luego menos mayúsculas.
  2. MANUAL: mapa curado para colabs/sufijos que la normalización no cubre
     ("Ghost B.C."→"Ghost", "bbno$ & Lentra"→"bbno$"). Al partir una colab
     se preserva el crédito completo en ARTIST_CREDIT (si no existía).

Por archivo: reescribe ARTIST (y ALBUMARTIST si matchea), mtime original +1s
(mismo día para crons -mtime, pero Navidrome sí ve el cambio), y actualiza
beets library.db para que `beet write` no revierta.

Uso:
    merge-artist-variants.py            # dry-run
    merge-artist-variants.py --apply    # aplica (backup de library.db antes)
"""

from __future__ import annotations

import argparse
import collections
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

# Colabs / sufijos que la normalización no detecta. Revisado a mano 2026-07-09
# (salida de scan-variants). El destino debe ser el nombre canónico final.
MANUAL = {
    "Ghost B.C.": "Ghost",
    "bbno$ & Lentra": "bbno$",
    "Darius & Duñe": "Darius",
    "Gorillaz & DRAM": "Gorillaz",
    "L’Impératrice & Cuco": "L’Impératrice",
    "L’Impératrice & Louve": "L’Impératrice",
    "mcbaise & Muthi": "mcbaise",
    "Purple Disco Machine & Kungs": "Purple Disco Machine",
    "Skinshape & Anina": "Skinshape",
    "Tricky & Marta": "Tricky",
    "Wilson Simonal & Som Três": "Wilson Simonal",
    "DJ Blyatman; длб": "DJ Blyatman",
    "potsu w/ 増子奈保": "potsu",
    "KSLV": "KSLV Noh",
    "MorMor Music": "MorMor",
    "Palmasur lofi": "Palmasur",
    "PomplamooseMusic": "Pomplamoose",
    "Tommy heavenly⁶": "Tommy heavenly6",
    "мой друг магнитофон & Свидетельство О Смерти": "мой друг магнитофон",
}
# NO tocar (parecen variante pero son artistas distintos): Bilal/Bilal Saeed,
# Cher/Cherokee, Dope/Dope Lemon, Junior/Junior Jack.

# Nombres oficiales/MusicBrainz que le ganan a la heurística de counts
# (si una variante del grupo está acá, es la canónica).
CANONICAL = {
    "Caravan Palace",
    "King Gizzard & the Lizard Wizard",
    "Maye",
    "L'Indécis",
    "Dwig",
    "half·alive",
}


def norm(s: str) -> str:
    """Forma normalizada: casefold, solo \\w unicode (letras/números)."""
    return re.sub(r"[^\w]+", "", s.casefold())


def pick_canonical(variants: list[tuple[str, int]]) -> str:
    """Elige el nombre canónico entre variantes (nombre, count)."""
    for name, _ in variants:
        if name in CANONICAL:
            return name

    def score(v: tuple[str, int]):
        name, count = v
        has_curly = "’" in name
        uppers = sum(1 for c in name if c.isupper())
        return (count, has_curly, -uppers)
    return max(variants, key=score)[0]


def build_mapping(counts: collections.Counter) -> dict[str, str]:
    groups = collections.defaultdict(list)
    for name, c in counts.items():
        k = norm(name)
        if k:
            groups[k].append((name, c))
    mapping: dict[str, str] = {}
    for variants in groups.values():
        if len(variants) < 2:
            continue
        canon = pick_canonical(variants)
        for name, _ in variants:
            if name != canon:
                mapping[name] = canon
    # Manual pisa/complementa; destino pasa por el mapa de case por si acaso.
    for old, new in MANUAL.items():
        mapping[old] = mapping.get(new, new)
    return mapping


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Aplicar cambios (default: dry-run)")
    ap.add_argument("--music-dir", type=Path, default=MUSIC_DIR)
    ap.add_argument("--beets-db", type=Path, default=BEETS_DB)
    args = ap.parse_args()

    # Pasada 1: censo de artistas
    counts: collections.Counter = collections.Counter()
    tags: dict[Path, OggOpus] = {}
    for opus in sorted(args.music_dir.glob("*.opus")):
        try:
            f = OggOpus(str(opus))
        except Exception:
            continue
        tags[opus] = f
        art = (f.get("artist") or [""])[0].strip()
        if art:
            counts[art] += 1

    mapping = build_mapping(counts)
    if not mapping:
        print("Nada que unificar.")
        return 0

    print("== Mapa de unificación ==")
    for old, new in sorted(mapping.items(), key=lambda kv: kv[1].casefold()):
        tag_ = " [manual]" if old in MANUAL else ""
        print(f"  {old!r} → {new!r}{tag_}")

    con = None
    if args.apply and args.beets_db.exists():
        bak = args.beets_db.with_suffix(f".db.bak-merge-{time.strftime('%Y%m%d')}")
        shutil.copy2(args.beets_db, bak)
        print(f"[beets] backup: {bak}")
        con = sqlite3.connect(args.beets_db)

    changed = 0
    for opus, f in tags.items():
        artist = (f.get("artist") or [""])[0].strip()
        albumartist = (f.get("albumartist") or [""])[0].strip()
        new_a = mapping.get(artist)
        new_aa = mapping.get(albumartist)
        if not new_a and not new_aa:
            continue
        changed += 1
        print(f"  {opus.name}: {artist!r} → {(new_a or artist)!r}")
        if not args.apply:
            continue
        st = opus.stat()
        if new_a:
            f["artist"] = [new_a]
            # Colab partida (manual con "&"/"w/"/";") → preservar crédito
            if artist in MANUAL and artist != MANUAL[artist] \
               and norm(artist) != norm(new_a) and "artist_credit" not in f:
                f["artist_credit"] = [artist]
        if new_aa:
            f["albumartist"] = [new_aa]
        f.save()
        os.utime(opus, (st.st_atime, st.st_mtime + 1))
        if con is not None:
            con.execute(
                """UPDATE items SET
                     artist = CASE WHEN ? != '' THEN ? ELSE artist END,
                     artists = CASE WHEN ? != '' THEN ? ELSE artists END,
                     albumartist = CASE WHEN ? != '' THEN ? ELSE albumartist END,
                     albumartists = CASE WHEN ? != '' THEN ? ELSE albumartists END
                   WHERE path = ?""",
                (new_a or "", new_a or "", new_a or "", new_a or "",
                 new_aa or "", new_aa or "", new_aa or "", new_aa or "",
                 opus.name.encode()))

    if con is not None:
        con.commit()
        con.close()

    mode = "APLICADO" if args.apply else "DRY-RUN"
    print(f"[{mode}] archivos afectados: {changed} | "
          f"variantes unificadas: {len(mapping)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
