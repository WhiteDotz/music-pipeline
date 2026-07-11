#!/bin/bash
# weekly-downloads.sh - M3U con tracks descargados los últimos N días,
# excluyendo los que ya están en liked-music.m3u
#
# Idea: ver de un solo vistazo lo nuevo de la semana sin re-listar
# las canciones que ya tenés "guardadas" (liked).
#
# Uso:
#   weekly-downloads.sh             # 7 días default
#   weekly-downloads.sh --days 14
#
# Output: ~/storage/Music/playlists/weekly-downloads.m3u

set -euo pipefail

DAYS="${DAYS:-7}"
while [ $# -gt 0 ]; do
    case "$1" in
        --days) DAYS="$2"; shift 2;;
        *) echo "Flag desconocida: $1" >&2; exit 2;;
    esac
done

MUSIC_DIR="$HOME/storage/Music"
PLAYLISTS_DIR="$MUSIC_DIR/playlists"
LIKED_M3U="$PLAYLISTS_DIR/liked-music.m3u"
OUT="$PLAYLISTS_DIR/weekly-downloads.m3u"

mkdir -p "$PLAYLISTS_DIR"

# Set de paths que están en liked-music.m3u (relativos, como los emite playlist-m3u.py)
LIKED_SET=$(mktemp)
trap 'rm -f "$LIKED_SET"' EXIT
if [ -f "$LIKED_M3U" ]; then
    grep -v '^#' "$LIKED_M3U" | grep -v '^\s*$' | sort -u > "$LIKED_SET"
    echo "[weekly-dl] excluyendo $(wc -l < "$LIKED_SET") tracks en liked-music" >&2
else
    echo "[weekly-dl] $LIKED_M3U no existe — no se excluye nada" >&2
fi

# Find .opus modificados últimos $DAYS días, no en .trash/Music_archive.
# Output: paths relativos a MUSIC_DIR, comparables con liked-music.m3u
# (que usa formato "../Title.opus" desde playlists/).
TMP_FOUND=$(mktemp)
trap 'rm -f "$LIKED_SET" "$TMP_FOUND"' EXIT
find "$MUSIC_DIR" -maxdepth 1 -type f -name "*.opus" -mtime "-$DAYS" \
    -printf "../%f\n" | sort > "$TMP_FOUND"

TOTAL=$(wc -l < "$TMP_FOUND")
KEPT=$(comm -23 "$TMP_FOUND" "$LIKED_SET" | wc -l)
echo "[weekly-dl] últimos ${DAYS}d: $TOTAL tracks, $((TOTAL - KEPT)) excluidos, $KEPT quedan" >&2

{
    echo "#EXTM3U"
    echo "#PLAYLIST:Weekly Downloads (last ${DAYS}d)"
    comm -23 "$TMP_FOUND" "$LIKED_SET"
} > "$OUT"

echo "[weekly-dl] escrito: $OUT" >&2
