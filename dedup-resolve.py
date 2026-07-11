#!/usr/bin/env python3
"""dedup-resolve.py — Resuelve duplicados detectados por dedup-music.py.

Lee /tmp/dedup-report.tsv (salida de dedup-music.py) y procesa:
  - "dupe": audio idéntico → borra el (N).opus.
  - "different" con |duración| ≤ 3s: re-encode del mismo tema (mismo título,
    distinto upload/videoId) → borra el (N).opus, conserva el base.
  - "different" con duración distinta, "version", "orphan", "error": NO toca.

Contabilidad por cada archivo eliminado (lo que dedup-music.py no hacía):
  1. Mueve a la carpeta .trash-dedup-* más reciente (tiene .ndignore).
  2. Redirige su videoId en .video_id_index.json → archivo conservado
     (playlists regeneradas resuelven ambos vids al mismo archivo).
  3. Reescribe TODOS los M3U de playlists/: línea del borrado → conservado,
     y dedup de líneas repetidas (conserva primera aparición).
  4. Borra la fila del beets library.db (backup antes).

Uso:
    dedup-resolve.py            # dry-run
    dedup-resolve.py --apply
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

try:
    import mediafile
except ImportError:
    print("❌ mediafile no disponible (usar python del venv)", file=sys.stderr)
    sys.exit(2)

MUSIC_DIR = Path.home() / "storage/Music"
PLAYLISTS = MUSIC_DIR / "playlists"
INDEX = MUSIC_DIR / ".video_id_index.json"
BEETS_DB = Path.home() / "scripts/beets/library.db"
REPORT = Path("/tmp/dedup-report.tsv")
DUR_TOL = 3.0  # segundos


def duration(p: Path) -> float | None:
    try:
        return mediafile.MediaFile(str(p)).length
    except Exception:
        return None


def video_id(p: Path) -> str | None:
    try:
        mf = mediafile.MediaFile(str(p))
    except Exception:
        return None
    raw = getattr(mf, "mgfile", None)
    cands = []
    if raw is not None:
        for key in ("purl", "PURL", "comment", "COMMENT"):
            v = raw.get(key)
            if v:
                cands.extend(v if isinstance(v, list) else [v])
    for c in cands:
        m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", str(c))
        if m:
            return m.group(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not REPORT.exists():
        print(f"❌ Falta {REPORT} — correr dedup-music.py primero", file=sys.stderr)
        return 2

    # (dup a borrar, pair a conservar, motivo)
    to_remove: list[tuple[str, str, str]] = []
    kept_diff = 0
    with REPORT.open() as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            dup, pair = row["dup_file"], row["pair_file"]
            if row["type"] == "dupe":
                to_remove.append((dup, pair, "idéntico"))
            elif row["type"] == "different":
                d1, d2 = duration(MUSIC_DIR / dup), duration(MUSIC_DIR / pair)
                if d1 and d2 and abs(d1 - d2) <= DUR_TOL:
                    to_remove.append((dup, pair, f"re-encode Δ{abs(d1-d2):.1f}s"))
                else:
                    kept_diff += 1

    print(f"A eliminar: {len(to_remove)} | different conservados "
          f"(duración distinta): {kept_diff}")
    for dup, pair, why in to_remove:
        print(f"  {dup}  →  {pair}  [{why}]")

    if not args.apply:
        print("\nDry-run. Aplicar con --apply")
        return 0

    trashes = sorted(MUSIC_DIR.glob(".trash-dedup-*"))
    trash = trashes[-1] if trashes else MUSIC_DIR / f".trash-dedup-{int(time.time())}"
    trash.mkdir(exist_ok=True)
    (trash / ".ndignore").touch()

    index = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    bak = BEETS_DB.with_suffix(f".db.bak-dedup-{time.strftime('%Y%m%d')}")
    shutil.copy2(BEETS_DB, bak)
    print(f"[beets] backup: {bak}")
    con = sqlite3.connect(BEETS_DB)

    renames: dict[str, str] = {}  # dup_name → pair_name (para M3Us)
    for dup, pair, _ in to_remove:
        src = MUSIC_DIR / dup
        if not src.exists() or not (MUSIC_DIR / pair).exists():
            continue
        vid = video_id(src)
        src.rename(trash / dup)
        if vid:
            index[vid] = pair
        con.execute("DELETE FROM items WHERE path = ?", (dup.encode(),))
        renames[dup] = pair

    con.commit()
    con.close()
    INDEX.write_text(json.dumps(index, indent=2, sort_keys=True))
    print(f"[index] {len(index)} entradas (redirects aplicados)")

    # Reescribir M3Us: sustituir path del borrado y dedup de líneas
    for m3u in sorted(PLAYLISTS.glob("*.m3u")):
        lines = m3u.read_text().splitlines()
        out, seen, changed = [], set(), False
        for line in lines:
            if line.startswith("#"):
                out.append(line)
                continue
            name = line.rsplit("/", 1)[-1]
            if name in renames:
                line = line[: len(line) - len(name)] + renames[name]
                changed = True
            if line in seen:
                changed = True
                continue
            seen.add(line)
            out.append(line)
        if changed:
            m3u.write_text("\n".join(out) + "\n")
            print(f"[m3u] reescrito: {m3u.name}")

    print(f"\n✓ {len(renames)} duplicados movidos a {trash}")
    print("  Falta: scan Navidrome + Poweramp rescan en cel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
