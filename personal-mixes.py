#!/usr/bin/env python3
"""personal-mixes.py - Mixes personales con 75% descubrimiento + 25% propios.

Flow:
  1. Query Navidrome → candidatos (play_count>=N o starred=1).
  2. Pasa top genres/artistas a Gemini → propone N mixes (slug, nombre,
     patron_genres regex, descripcion).
  3. Para cada mix:
     a. Filtra candidates por regex → top OWN tracks por score.
     b. Top 3-4 artistas propios en el mix → seeds para fanout.
     c. fanout (con --related-only): ~15 NUEVOS de related artists.
     d. URLs file = 15 nuevos + ~5 propios.
     e. dl-playlist.sh --urls-file → descarga nuevos, skipea propios
        (ya en archive), genera M3U personal-mix-<slug>.m3u con los 20.

Ratio default 75% nuevos / 25% propios (configurable via env).
Cron: 0 6 * * 5  (viernes 06:00)

Vars:
  NAVIDROME_DB, BEETS_DB, MUSIC_DIR
  MIN_PLAYS=1, MIX_SIZE=20, NUM_MIXES=6
  NEW_RATIO=0.75  → 15 nuevos + 5 propios para MIX_SIZE=20
  GEMINI, FANOUT, DL_PLAYLIST  (paths binarios)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import argparse
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

NAVIDROME_DB = Path(os.environ.get(
    "NAVIDROME_DB", str(Path.home() / "docker/navidrome/data/navidrome.db")))
BEETS_DB = Path(os.environ.get(
    "BEETS_DB", str(Path.home() / "scripts/beets/library.db")))
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", str(Path.home() / "storage/Music")))
PLAYLISTS_DIR = MUSIC_DIR / "playlists"
VIDEO_ID_INDEX = MUSIC_DIR / ".video_id_index.json"

MIN_PLAYS = int(os.environ.get("MIN_PLAYS", "1"))
MIX_SIZE = int(os.environ.get("MIX_SIZE", "20"))
NUM_MIXES = int(os.environ.get("NUM_MIXES", "6"))
NEW_RATIO = float(os.environ.get("NEW_RATIO", "0.75"))

# Default migrado a Antigravity CLI (agy) el 2026-06-26: Google deshabilitó el
# tier gratis del OAuth de gemini-cli (18-jun-2026). Mismo flag -p. Override con
# la env var GEMINI si se quiere otro binario.
GEMINI = os.environ.get("GEMINI", str(Path.home() / ".local/bin/agy"))
FANOUT = os.environ.get(
    "FANOUT", str(Path.home() / "scripts/gemini-explorer/fanout.py"))
DL_PLAYLIST = os.environ.get(
    "DL_PLAYLIST", str(Path.home() / "scripts/dl-playlist.sh"))
PYTHON = os.environ.get(
    "PYTHON", str(Path.home() / "scripts/.venv/bin/python"))


def load_candidates() -> list[dict]:
    """Tracks favoritos por play o starred. Trae genres de beets."""
    ndb = sqlite3.connect(f"file:{NAVIDROME_DB}?mode=ro", uri=True)
    bdb = sqlite3.connect(f"file:{BEETS_DB}?mode=ro", uri=True)

    ncur = ndb.cursor()
    ncur.execute("""
        SELECT mf.path, mf.title, mf.artist, mf.album,
               COALESCE(a.play_count, 0) AS plays,
               COALESCE(a.starred, 0) AS starred
        FROM media_file mf
        LEFT JOIN annotation a
          ON a.item_id = mf.id AND a.item_type = 'media_file'
        WHERE (COALESCE(a.play_count, 0) >= ? OR COALESCE(a.starred, 0) = 1)
          AND mf.path NOT LIKE '.trash%'
          AND mf.path LIKE '%.opus'
    """, (MIN_PLAYS,))
    rows = ncur.fetchall()

    bcur = bdb.cursor()
    candidates: list[dict] = []
    for path, title, artist, album, plays, starred in rows:
        bcur.execute("SELECT genres FROM items WHERE path = ?", (path.encode(),))
        bres = bcur.fetchone()
        genres = bres[0] if bres else ""
        candidates.append({
            "path": path, "title": title, "artist": artist, "album": album,
            "plays": plays, "starred": starred, "genres": genres,
            "score": plays + (5 if starred else 0),
        })

    ndb.close()
    bdb.close()
    return candidates


def top_genres_weighted(candidates: list[dict], n: int = 20) -> list[tuple[str, int]]:
    weighted: Counter[str] = Counter()
    for c in candidates:
        if not c["genres"]:
            continue
        for g in re.split(r"[;,]", c["genres"]):
            g = g.strip()
            if g:
                weighted[g] += c["score"]
    return weighted.most_common(n)


def ask_gemini_mixes(candidates: list[dict], num_mixes: int) -> list[dict]:
    genres_summary = top_genres_weighted(candidates, n=25)
    artist_scores: Counter[str] = Counter()
    for c in candidates:
        artist_scores[c["artist"]] += c["score"]
    top_artists = [a for a, _ in artist_scores.most_common(30)]

    prompt = f"""Sos un curador musical. Resumen del listening de un usuario:

TOP GÉNEROS (peso por plays+star):
{json.dumps(genres_summary[:25], ensure_ascii=False, indent=2)}

TOP ARTISTAS:
{json.dumps(top_artists, ensure_ascii=False)}

Proponé {num_mixes} "personal mixes" distintos basados en estos gustos. Cada mix
será 75% música NUEVA descubierta + 25% del listening propio.

Los SEEDS de descubrimiento se buscarán en MusicBrainz (DB de metadata
canónica) — vos especificás tags y país. MusicBrainz devolverá los artistas
canónicos del género/región solicitados.

Para cada mix dame:
- slug: kebab-case corto (ej: "mix-metal-pesado", "mix-city-pop").
- nombre: título legible.
- descripcion: 1 línea humana.
- mb_tags: lista de tags MusicBrainz que definen el género (lowercase, separar
  con guiones si tienen espacios). Ej: ["krautrock"], ["mpb", "samba"],
  ["city-pop"], ["heavy-metal", "doom-metal"], ["shoegaze", "dream-pop"].
- mb_country: código ISO 3166-1 alpha-2 si el mix es geográfico (ej. "BR",
  "DE", "JP"). Si el mix es global/género agnóstico de país, "" (string vacío).
- mb_year_from / mb_year_to: int o null. Si el mix es de era específica
  (ej. "city pop 80s" → 1978-1989). Si no, null.
- patron_genres: regex case-insensitive Python sobre genres de la library
  del usuario, para filtrar los 25% propios. Ej: "metal|doom|sludge|stoner".

FORMATO OUTPUT: JSON ESTRICTO sin markdown wrapper. Ej:
{{
  "mixes": [
    {{"slug": "mix-krautrock", "nombre": "Mix Krautrock",
      "descripcion": "Pioneros alemanes del rock experimental 70s",
      "mb_tags": ["krautrock"], "mb_country": "DE",
      "mb_year_from": 1968, "mb_year_to": 1980,
      "patron_genres": "krautrock|kosmische|electronic"}},
    ...
  ]
}}
"""

    proc = subprocess.run(
        [GEMINI, "-p", prompt],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        print(f"ERROR: gemini exit {proc.returncode}", file=sys.stderr)
        print(proc.stderr[:500], file=sys.stderr)
        sys.exit(2)

    raw = proc.stdout.strip()
    raw = re.sub(r"^```(?:json)?\n", "", raw)
    raw = re.sub(r"\n```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: Gemini no devolvió JSON válido: {e}", file=sys.stderr)
        print(raw[:500], file=sys.stderr)
        sys.exit(3)
    return data.get("mixes", [])


MB_API = "https://musicbrainz.org/ws/2"
MB_UA = "hp15-personal-mixes/1.0 (contact@example.com)"
_mb_last_call = [0.0]


def mb_query_artists(tags: list[str], country: str = "",
                     year_from: int | None = None,
                     year_to: int | None = None,
                     limit: int = 8) -> list[str]:
    """Query MB para artistas matcheando tags + country + era opcional.

    Devuelve lista de nombres ordenados por score MB.
    Respeta rate limit MB (1 req/seg)."""
    if not tags:
        return []

    parts = []
    tag_clause = " OR ".join(f'tag:"{t}"' for t in tags)
    parts.append(f"({tag_clause})")
    if country:
        parts.append(f"country:{country}")
    if year_from or year_to:
        yf = year_from or 1900
        yt = year_to or 2100
        parts.append(f"begin:[{yf} TO {yt}]")
    query = " AND ".join(parts)

    # Rate limit
    elapsed = time.time() - _mb_last_call[0]
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)

    url = f"{MB_API}/artist/?query={urllib.parse.quote(query)}&fmt=json&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": MB_UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  [WARN] MB query falló ({query}): {e}", file=sys.stderr)
        _mb_last_call[0] = time.time()
        return []
    _mb_last_call[0] = time.time()

    # Filtros: score alto (>=80 reduce falsos positivos como Taylor Swift en
    # shoegaze), excluir "Various Artists" y similares pseudo-artistas.
    BLACKLIST = {"various artists", "[anonymous]", "[unknown]", "various"}
    artists = []
    for a in data.get("artists", []):
        name = a.get("name", "").strip()
        if not name or name.lower() in BLACKLIST:
            continue
        if a.get("score", 0) < 80:
            continue
        artists.append(name)
    return artists[:limit]


def filter_mix_candidates(candidates: list[dict], mix: dict) -> list[dict]:
    """Candidates del usuario que matchean el mix regex. Sorted por score desc."""
    rx_g = re.compile(mix.get("patron_genres", ""), re.IGNORECASE) if mix.get("patron_genres") else None

    matched = []
    for c in candidates:
        if rx_g and rx_g.search(c["genres"] or ""):
            matched.append(c)
    matched.sort(key=lambda x: (-x["score"], -x["starred"], -x["plays"]))
    return matched


def reverse_index() -> dict[str, str]:
    """filename → video_id usando ~/storage/Music/.video_id_index.json"""
    if not VIDEO_ID_INDEX.exists():
        return {}
    data = json.loads(VIDEO_ID_INDEX.read_text())
    return {v: k for k, v in data.items()}


def run_fanout(slug: str, seeds: list[str], max_tracks: int) -> list[tuple[str, str, str]]:
    """Invoca fanout.py --related-only para obtener tracks nuevos.

    Devuelve list of (video_id, title, artist). Retorna [] si fanout falla
    o devuelve menos del mínimo."""
    eje_file = MUSIC_DIR / f".tmp-personal-eje-{slug}.json"
    out_file = MUSIC_DIR / f".tmp-personal-urls-{slug}.txt"
    eje_file.write_text(json.dumps({"slug": slug, "seeds": seeds}), encoding="utf-8")

    rc = subprocess.run([
        PYTHON, FANOUT,
        "--eje", str(eje_file),
        "--out", str(out_file),
        "--max-tracks", str(max_tracks),
        "--related-only",
    ], capture_output=True, text=True)

    eje_file.unlink(missing_ok=True)

    if rc.returncode != 0:
        print(f"  [WARN] fanout {slug} exit {rc.returncode}: {rc.stderr[:200]}",
              file=sys.stderr)
        out_file.unlink(missing_ok=True)
        return []

    tracks = []
    if out_file.exists():
        for line in out_file.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                tracks.append((parts[0], parts[1], parts[2]))
        out_file.unlink(missing_ok=True)
    return tracks


def write_urls_file(out_path: Path, entries: list[tuple[str, str, str]]) -> None:
    """Escribe en formato compatible con dl-playlist.sh --urls-file."""
    out_path.write_text(
        "\n".join(f"{vid}\t{title}\t{artist}" for vid, title, artist in entries) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="No invocar dl-playlist; solo imprimir lo que descargaría")
    args = ap.parse_args()

    PLAYLISTS_DIR.mkdir(parents=True, exist_ok=True)
    new_count = max(1, round(MIX_SIZE * NEW_RATIO))
    own_count = MIX_SIZE - new_count
    print(f"[personal-mixes] config: {MIX_SIZE} tracks/mix "
          f"({new_count} nuevos + {own_count} propios)", file=sys.stderr)

    print(f"[personal-mixes] cargando candidatos (play>={MIN_PLAYS} o starred)...",
          file=sys.stderr)
    candidates = load_candidates()
    if not candidates:
        print("ERROR: no hay candidatos.", file=sys.stderr)
        return 2
    print(f"[personal-mixes] {len(candidates)} candidatos", file=sys.stderr)

    print(f"[personal-mixes] consultando Gemini para {NUM_MIXES} mixes...",
          file=sys.stderr)
    mixes = ask_gemini_mixes(candidates, NUM_MIXES)
    print(f"[personal-mixes] Gemini propuso {len(mixes)} mixes", file=sys.stderr)

    rev_idx = reverse_index()
    if not rev_idx:
        print("WARN: video_id_index vacío — tracks propios no entrarán al M3U",
              file=sys.stderr)

    success = 0
    for mix in mixes:
        slug = mix.get("slug", "").strip()
        if not slug:
            continue
        print(f"\n[mix] {slug}: {mix.get('nombre', '')}", file=sys.stderr)

        # Tracks propios del usuario para este mix (25%)
        own_matches = filter_mix_candidates(candidates, mix)
        own_top = own_matches[:own_count]
        print(f"  propios: {len(own_top)}/{own_count} matchean regex",
              file=sys.stderr)

        # Seeds: query MusicBrainz por tags + country + era. Más preciso
        # que regex sobre genres del usuario (que da falsos positivos como
        # Khruangbin tageado 'tropical' en mix Brasil).
        mb_tags = mix.get("mb_tags", []) or []
        mb_country = mix.get("mb_country", "") or ""
        yr_from = mix.get("mb_year_from")
        yr_to = mix.get("mb_year_to")
        seen_artists = mb_query_artists(mb_tags, mb_country, yr_from, yr_to,
                                        limit=6)
        if not seen_artists:
            # Fallback: top artistas propios que matcheen el regex
            for c in own_matches:
                if c["artist"] and c["artist"] not in seen_artists:
                    seen_artists.append(c["artist"])
                if len(seen_artists) >= 4:
                    break
            print(f"  [warn] MB devolvió 0 artistas — fallback a propios",
                  file=sys.stderr)
        if not seen_artists:
            print(f"  [SKIP] sin seeds disponibles", file=sys.stderr)
            continue
        print(f"  seeds (MB tags={mb_tags} country={mb_country}): {seen_artists[:6]}",
              file=sys.stderr)

        # Fanout para tracks nuevos
        new_tracks = run_fanout(slug, seen_artists, new_count)
        print(f"  nuevos: {len(new_tracks)}/{new_count} via fanout",
              file=sys.stderr)

        # Convertir propios a (video_id, title, artist) usando rev index
        own_entries: list[tuple[str, str, str]] = []
        for c in own_top:
            vid = rev_idx.get(c["path"])
            if vid:
                own_entries.append((vid, c["title"] or c["path"], c["artist"] or ""))

        # Combinar
        all_entries = new_tracks + own_entries
        if len(all_entries) < 5:
            print(f"  [SKIP] solo {len(all_entries)} entries, mínimo 5",
                  file=sys.stderr)
            continue

        # Display: "Mix: <nombre>". M3U se nombra slugificando esto
        # (playlist-m3u.py) → "mix-<nombre>.m3u". cleanup clasifica por
        # prefijo "Mix:" (regex ^Mix:).
        nombre = mix.get("nombre", "").strip() or slug
        playlist_name = f"Mix: {nombre}"

        if args.dry_run:
            print(f"  [DRY] {playlist_name}: {len(new_tracks)} nuevos + "
                  f"{len(own_entries)} propios = {len(all_entries)} total",
                  file=sys.stderr)
            print("  Sample nuevos:", file=sys.stderr)
            for vid, title, artist in new_tracks[:5]:
                print(f"    + {artist} — {title} ({vid})", file=sys.stderr)
            print("  Sample propios:", file=sys.stderr)
            for vid, title, artist in own_entries[:5]:
                print(f"    = {artist} — {title} ({vid})", file=sys.stderr)
            success += 1
            continue

        urls_file = MUSIC_DIR / f".tmp-personal-mix-{slug}-urls.txt"
        write_urls_file(urls_file, all_entries)
        print(f"  [DL] {len(all_entries)} entries → {playlist_name}", file=sys.stderr)

        rc = subprocess.run(
            [DL_PLAYLIST, "--urls-file", str(urls_file), playlist_name],
            capture_output=True, text=True,
        )
        urls_file.unlink(missing_ok=True)

        if rc.returncode != 0:
            print(f"  [ERR] dl-playlist falló: {rc.stderr[-300:]}",
                  file=sys.stderr)
            continue
        success += 1
        print(f"  [OK]  {playlist_name}", file=sys.stderr)

    print(f"\n[personal-mixes] {success}/{len(mixes)} mixes generados",
          file=sys.stderr)
    return 0 if success > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
