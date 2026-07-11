#!/bin/bash
# ============================================================================
# dl-single.sh — descarga UN track (URL o query texto) usando pipeline existente
#
# Uso:
#   ./dl-single.sh "https://music.youtube.com/watch?v=..."
#   ./dl-single.sh "blue monday new order"
#
# Aterriza en ~/storage/Music/ sin generar M3U (delega a dl-playlist.sh con
# --urls-file + --no-m3u). Beets enrich corre vía hook de dl-playlist.sh.
#
# Detección input: regex ^https?:// → URL; resto → query ytmusicapi.search.
# ============================================================================

set -euo pipefail

INPUT="${1:-}"
if [ -z "$INPUT" ]; then
    echo "Uso: $0 <url|query>" >&2
    exit 2
fi

PYTHON="$HOME/scripts/.venv/bin/python"
YTDLP="$HOME/scripts/.venv/bin/yt-dlp"
BUN="$HOME/.bun/bin/bun"
COOKIES_FILE="$HOME/scripts/youtube-cookies.txt"
DL_PLAYLIST="$HOME/scripts/dl-playlist.sh"
TMP_FILE=$(mktemp /tmp/dl-single-XXXX.tsv)
trap 'rm -f "$TMP_FILE"' EXIT

resolve_url() {
    local url="$1"
    # --playlist-items 1 protege contra URLs que llevan &list=...
    "$YTDLP" --cookies "$COOKIES_FILE" \
        --no-js-runtimes --js-runtimes "bun:$BUN" --remote-components ejs:github \
        --no-warnings --flat-playlist --playlist-items 1 \
        --print "%(id)s	%(title)s	%(uploader|)s" \
        "$url" 2>/dev/null | head -1
}

resolve_query() {
    local q="$1"
    "$PYTHON" - "$q" <<'PYEOF'
import sys
from ytmusicapi import YTMusic
yt = YTMusic()
q = sys.argv[1]
results = yt.search(q, filter="songs", limit=1)
if not results:
    sys.exit(3)
r = results[0]
vid = r.get("videoId") or ""
title = r.get("title") or ""
artists = ", ".join(a.get("name", "") for a in (r.get("artists") or []) if a.get("name"))
if not vid:
    sys.exit(3)
print(f"{vid}\t{title}\t{artists}")
PYEOF
}

if [[ "$INPUT" =~ ^https?:// ]]; then
    echo "🔗 Modo URL"
    LINE=$(resolve_url "$INPUT")
else
    echo "🔎 Modo query (ytmusicapi)"
    LINE=$(resolve_query "$INPUT")
fi

if [ -z "$LINE" ]; then
    echo "❌ No se pudo resolver: $INPUT" >&2
    exit 1
fi

echo "🎵 Track: $LINE"
echo "$LINE" > "$TMP_FILE"

# Delegate al pipeline. "_single" es nombre interno (no genera M3U por --no-m3u).
"$DL_PLAYLIST" --urls-file "$TMP_FILE" "_single" --no-m3u
