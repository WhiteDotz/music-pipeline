#!/usr/bin/env python3
"""playlist-m3u.py — genera M3U para Navidrome a partir de IDs YouTube.

Construye un índice video_id → ruta_archivo leyendo tags Vorbis 'purl' /
'comment' que yt-dlp embebe en cada .opus. Usa mediafile (libreria de beets,
in-process, sin spawn por archivo). Cachea el índice en JSON dentro del
directorio de música y solo escanea archivos nuevos en runs siguientes.

Uso:
    playlist-m3u.py --music-dir ~/storage/Music \\
                    --ids-file /tmp/playlist_$$.txt \\
                    --name "Mi Playlist" \\
                    [--m3u-out ~/storage/Music/playlists/mi-playlist.m3u]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    import mediafile
except ImportError:
    print("❌ mediafile no instalado. pip install mediafile", file=sys.stderr)
    sys.exit(2)

INDEX_FILENAME = ".video_id_index.json"
PURL_RE = re.compile(r"(?:v=|youtu\.be/|/watch\?v=)([\w-]{11})")
SAVE_EVERY = 200  # persiste el índice cada N archivos (recovery si interrumpe)


def slugify(name: str) -> str:
    s = re.sub(r"[^\w\s-]", "", name, flags=re.UNICODE).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s or "playlist"


def extract_video_id(s: str) -> str | None:
    m = PURL_RE.search(s or "")
    return m.group(1) if m else None


def read_video_id(opus_path: Path) -> str | None:
    try:
        mf = mediafile.MediaFile(str(opus_path))
    except mediafile.UnreadableFileError:
        return None
    # mediafile expone 'comments' (lyrics-style) y los tags raw vía .mgfile
    # El tag 'purl' es no-estándar — accedemos al backend mutagen directo.
    candidates = []
    if mf.comments:
        candidates.append(mf.comments)
    if mf.url:
        candidates.append(mf.url)
    # Acceso raw al objeto Mutagen (Vorbis Comment dict)
    raw = getattr(mf, "mgfile", None)
    if raw is not None:
        for key in ("purl", "PURL", "comment", "COMMENT", "url", "URL", "website"):
            try:
                vals = raw.get(key)
            except Exception:
                vals = None
            if vals:
                candidates.extend(vals if isinstance(vals, list) else [vals])
    for c in candidates:
        vid = extract_video_id(str(c))
        if vid:
            return vid
    return None


def build_index(music_dir: Path, index_path: Path, prev: dict[str, str],
                rebuild: bool = False) -> dict[str, str]:
    """Escanea solo archivos no presentes en `prev` (si rebuild=False)."""
    index: dict[str, str] = {} if rebuild else dict(prev)
    indexed_paths = set(index.values())

    files = sorted(p for p in music_dir.glob("*.opus"))
    to_scan = [p for p in files if p.name not in indexed_paths] if not rebuild else files

    if not to_scan:
        print(f"[index] 0 nuevos, {len(index)} ya indexados", file=sys.stderr)
    else:
        print(f"[index] escaneando {len(to_scan)} archivos nuevos "
              f"({len(index)} ya en cache)...", file=sys.stderr)

    for i, opus in enumerate(to_scan, 1):
        vid = read_video_id(opus)
        if vid:
            index[vid] = opus.name
        if i % SAVE_EVERY == 0:
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True))
            print(f"[index] checkpoint {i}/{len(to_scan)} ({len(index)} entradas)",
                  file=sys.stderr)

    # Limpia entradas cuyo archivo ya no existe
    existing = {p.name for p in files}
    cleaned = {vid: name for vid, name in index.items() if name in existing}

    if cleaned != prev:
        index_path.write_text(json.dumps(cleaned, indent=2, sort_keys=True))
        print(f"[index] guardado: {len(cleaned)} entradas", file=sys.stderr)
    return cleaned


def load_index(index_path: Path) -> dict[str, str]:
    if index_path.exists():
        try:
            return json.loads(index_path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--music-dir", required=True, type=Path)
    ap.add_argument("--ids-file", required=True, type=Path,
                    help="TSV con VIDEO_ID en col 1 (TEMP_LIST de dl-playlist.sh)")
    ap.add_argument("--name", required=True, help="Nombre legible de la playlist")
    ap.add_argument("--m3u-out", type=Path, default=None,
                    help="Path destino. Default: <music-dir>/playlists/<slug>.m3u")
    ap.add_argument("--rebuild-index", action="store_true",
                    help="Ignora cache y reconstruye el índice completo")
    args = ap.parse_args()

    music_dir: Path = args.music_dir.expanduser().resolve()
    if not music_dir.is_dir():
        print(f"❌ music-dir no existe: {music_dir}", file=sys.stderr)
        return 2

    index_path = music_dir / INDEX_FILENAME
    prev = {} if args.rebuild_index else load_index(index_path)
    index = build_index(music_dir, index_path, prev, rebuild=args.rebuild_index)

    if not args.ids_file.exists():
        print(f"❌ ids-file no existe: {args.ids_file}", file=sys.stderr)
        return 2

    wanted_ids: list[str] = []
    seen: set[str] = set()
    with args.ids_file.open() as fh:
        for line in fh:
            vid = line.split("\t", 1)[0].strip()
            if vid and vid not in seen:
                wanted_ids.append(vid)
                seen.add(vid)

    out_path = args.m3u_out or (music_dir / "playlists" / f"{slugify(args.name)}.m3u")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    found, missing = [], []
    for vid in wanted_ids:
        name = index.get(vid)
        if name:
            found.append(name)
        else:
            missing.append(vid)

    lines = ["#EXTM3U", f"#PLAYLIST:{args.name}"]
    for name in found:
        rel = os.path.relpath(music_dir / name, out_path.parent)
        lines.append(rel)
    out_path.write_text("\n".join(lines) + "\n")

    print(f"[m3u] {out_path.name}: {len(found)} encontradas, {len(missing)} sin match")
    if missing:
        sample = " ".join(missing[:5])
        more = f" ...(+{len(missing) - 5})" if len(missing) > 5 else ""
        print(f"[m3u] sin match: {sample}{more}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
