#!/bin/bash
# weekly-axis.sh - Elige eje temático de la semana, descarga música, genera M3U.
# Uso:
#   weekly-axis.sh                # corre con semana ISO actual
#   weekly-axis.sh --week 19      # fuerza semana específica (testing)
#   weekly-axis.sh --list         # muestra eje que correría sin ejecutar

set -euo pipefail

AXES_FILE="$HOME/scripts/music-axes.txt"
DL_PLAYLIST="$HOME/scripts/dl-playlist.sh"
YTDLP="$HOME/scripts/.venv/bin/yt-dlp"
COOKIES="$HOME/scripts/youtube-cookies.txt"
LOG="$HOME/storage/Music/weekly-axis.log"

ISO_WEEK=$(date +%V)
ISO_YEAR=$(date +%G)
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --week) ISO_WEEK="$2"; shift 2;;
        --year) ISO_YEAR="$2"; shift 2;;
        --list) DRY_RUN=1; shift;;
        *) echo "Flag desconocida: $1"; exit 2;;
    esac
done

mkdir -p "$(dirname "$LOG")"

# Lee ejes (filtra comentarios y vacías)
mapfile -t axes < <(grep -vE '^\s*(#|$)' "$AXES_FILE")
total="${#axes[@]}"
if [ "$total" -eq 0 ]; then
    echo "❌ Sin ejes en $AXES_FILE" | tee -a "$LOG"
    exit 1
fi

# Selección determinística: semana % total
idx=$(( 10#$ISO_WEEK % total ))
line="${axes[$idx]}"
slug=$(echo "$line" | awk -F'|' '{print $1}' | xargs)
desc=$(echo "$line" | awk -F'|' '{print $2}' | xargs)
src=$(echo "$line"  | awk -F'|' '{print $3}' | xargs)

playlist_name="Semana-${ISO_YEAR}-W${ISO_WEEK}-${slug}"

echo "[$(date -Iseconds)] eje semana ${ISO_YEAR}-W${ISO_WEEK} (idx $idx/$total): $slug — $desc" | tee -a "$LOG"
echo "  fuente: $src" | tee -a "$LOG"
echo "  playlist destino: $playlist_name" | tee -a "$LOG"

if [ "$DRY_RUN" -eq 1 ]; then
    exit 0
fi

# Resolver fuente → URL utilizable por dl-playlist.sh
case "$src" in
    search:*)
        query="${src#search:}"
        # ytsearch produce IDs; los convertimos a una "playlist" virtual.
        # Tomamos N tracks con yt-dlp y los pasamos como URLs individuales no es ideal.
        # En lugar de eso, generamos archivo de IDs y pasamos directamente — pero dl-playlist
        # espera URL de playlist. Workaround: usar URL ytsearchN:query (yt-dlp lo trata como playlist).
        N=30
        url="ytsearch${N}:${query}"
        ;;
    http*)
        url="$src"
        ;;
    *)
        echo "❌ Fuente no reconocida: $src" | tee -a "$LOG"
        exit 1
        ;;
esac

echo "[$(date -Iseconds)] descargando $url ..." | tee -a "$LOG"
"$DL_PLAYLIST" "$url" "$playlist_name" 2>&1 | tee -a "$LOG"
