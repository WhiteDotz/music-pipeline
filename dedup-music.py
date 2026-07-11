#!/usr/bin/env python3
# Dedup Music/: para cada Track(N).opus busca par Track.opus (o Track(N-1).opus)
# y compara hash audio-only (ignora tags/cover). Reporta: dupe / version / orphan.
#
# Uso:
#   dedup-music.py            # dry-run, escribe /tmp/dedup-report.tsv
#   dedup-music.py --apply    # borra los marcados como dupe (mueve a .trash-dedup-TS/)

import os, re, sys, glob, json, hashlib, subprocess
from pathlib import Path
from collections import defaultdict

DIR = Path.home() / "storage/Music"
REPORT = Path("/tmp/dedup-report.tsv")
TRASH = DIR / f".trash-dedup-{int(__import__('time').time())}"

# Palabras clave que indican versión distinta (NO borrar aunque hash difiera)
VERSION_HINTS = re.compile(
    r'\b(remix|cover|live|acoustic|unplugged|version|edit|remaster|mix|extended|radio|instrumental|karaoke|demo|bootleg|mashup|rework|remake|alternate|stripped|piano|orchestral|sped|slow)\b',
    re.I
)

PAREN_N = re.compile(r'\((\d+)\)\.opus$')

def audio_md5(path: Path) -> str:
    """MD5 sólo stream audio (ignora tags + cover art)."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-map", "0:a", "-f", "md5", "-"],
            capture_output=True, text=True, timeout=30
        )
        out = r.stdout.strip()
        if out.startswith("MD5="):
            return out[4:]
        return ""
    except Exception as e:
        return f"ERR:{e}"

def base_name(filename: str) -> str:
    """Track(1).opus → Track.opus"""
    return PAREN_N.sub(".opus", filename)

def get_tags(path: Path) -> dict:
    try:
        import mediafile
        m = mediafile.MediaFile(str(path))
        return {"title": m.title or "", "artist": m.artist or "", "album": m.album or ""}
    except Exception:
        return {"title": "", "artist": "", "album": ""}

def is_version(a_tags: dict, b_tags: dict) -> bool:
    """¿Alguno de los dos tiene hint de versión distinta?"""
    blob = " ".join(filter(None, [
        a_tags.get("title"), b_tags.get("title"),
        a_tags.get("album"), b_tags.get("album"),
    ]))
    return bool(VERSION_HINTS.search(blob))

def main():
    apply_mode = "--apply" in sys.argv
    os.chdir(DIR)

    dupes_n = sorted(f for f in glob.glob("*(*).opus") if PAREN_N.search(f))
    print(f"Candidatos (N).opus: {len(dupes_n)}", file=sys.stderr)

    rows = []
    stats = defaultdict(int)

    for i, dup_name in enumerate(dupes_n, 1):
        if i % 25 == 0:
            print(f"  procesados {i}/{len(dupes_n)}...", file=sys.stderr)
        dup_path = DIR / dup_name
        base_n = base_name(dup_name)

        # Buscar candidato par: base sin (N), o (N-1)
        candidates = [base_n]
        m = PAREN_N.search(dup_name)
        if m:
            n = int(m.group(1))
            if n > 1:
                candidates.append(PAREN_N.sub(f"({n-1}).opus", dup_name))

        pair = None
        for c in candidates:
            if (DIR / c).exists():
                pair = DIR / c
                break

        if not pair:
            stats["orphan"] += 1
            rows.append(("orphan", dup_name, "", "", ""))
            continue

        h_dup = audio_md5(dup_path)
        h_pair = audio_md5(pair)

        if not h_dup or not h_pair or h_dup.startswith("ERR") or h_pair.startswith("ERR"):
            stats["error"] += 1
            rows.append(("error", dup_name, pair.name, h_dup, h_pair))
            continue

        if h_dup == h_pair:
            stats["dupe"] += 1
            rows.append(("dupe", dup_name, pair.name, h_dup, h_pair))
        else:
            tags_d = get_tags(dup_path)
            tags_p = get_tags(pair)
            if is_version(tags_d, tags_p):
                stats["version"] += 1
                rows.append(("version", dup_name, pair.name,
                             f"{tags_d['title']}|{tags_d['album']}",
                             f"{tags_p['title']}|{tags_p['album']}"))
            else:
                stats["different"] += 1
                rows.append(("different", dup_name, pair.name, h_dup, h_pair))

    with REPORT.open("w") as f:
        f.write("type\tdup_file\tpair_file\tinfo_dup\tinfo_pair\n")
        for r in rows:
            f.write("\t".join(r) + "\n")

    print(f"\n=== Resumen ===", file=sys.stderr)
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}", file=sys.stderr)
    print(f"\nReporte completo: {REPORT}", file=sys.stderr)

    if apply_mode:
        TRASH.mkdir(exist_ok=True)
        moved = 0
        for r in rows:
            if r[0] == "dupe":
                src = DIR / r[1]
                if src.exists():
                    src.rename(TRASH / src.name)
                    moved += 1
        print(f"\n✓ Movidos {moved} dupes a {TRASH}", file=sys.stderr)
        print(f"  Verificar y luego: rm -rf '{TRASH}'", file=sys.stderr)
    else:
        print(f"\nDry-run. Para aplicar: {sys.argv[0]} --apply", file=sys.stderr)

if __name__ == "__main__":
    main()
