#!/bin/bash
# beets-enrich.sh - Re-tag canciones via MusicBrainz, agrega género/MBID/replaygain
# Uso:
#   beets-enrich.sh                          # importa todo lo nuevo en ~/storage/Music
#   beets-enrich.sh --since SECS             # solo archivos modificados desde SECS epoch
#   beets-enrich.sh --files file1 file2 ...  # archivos específicos

set -euo pipefail

export BEETSDIR="$HOME/scripts/beets"
BEET="$HOME/scripts/.venv/bin/beet"
MUSIC_DIR="$HOME/storage/Music"
LOG="$MUSIC_DIR/beets.log"

if [ ! -x "$BEET" ]; then
    echo "❌ beets no instalado en $BEET" | tee -a "$LOG"
    exit 1
fi

mkdir -p "$BEETSDIR"
touch "$LOG"

echo "[$(date -Iseconds)] beets-enrich start" >> "$LOG"

mode="${1:-all}"

case "$mode" in
    --since)
        since_epoch="$2"
        echo "[$(date -Iseconds)] Modo --since $since_epoch (=$(date -d "@$since_epoch" -Iseconds))" >> "$LOG"
        mapfile -t files < <(find "$MUSIC_DIR" -maxdepth 1 -type f -name "*.opus" -newermt "@$since_epoch" -print)
        ;;
    --files)
        shift
        files=("$@")
        ;;
    all|"")
        mapfile -t files < <(find "$MUSIC_DIR" -maxdepth 1 -type f -name "*.opus" -print)
        ;;
    *)
        echo "❌ Modo desconocido: $mode" | tee -a "$LOG"
        exit 2
        ;;
esac

if [ "${#files[@]}" -eq 0 ]; then
    echo "[$(date -Iseconds)] No hay archivos nuevos para enriquecer" >> "$LOG"
    exit 0
fi

echo "[$(date -Iseconds)] Enriqueciendo ${#files[@]} archivos" >> "$LOG"

# nice + ionice para no saturar el N3060
nice -n 19 ionice -c 3 "$BEET" import --quiet --singletons "${files[@]}" >> "$LOG" 2>&1 || \
    echo "[$(date -Iseconds)] beet import terminó con warnings" >> "$LOG"

# Reescribe tags en archivos (algunos plugins solo modifican lib sin write inmediato)
nice -n 19 ionice -c 3 "$BEET" write >> "$LOG" 2>&1 || true

# Replay gain (lento — solo nuevos)
nice -n 19 ionice -c 3 "$BEET" replaygain --write -q "${files[@]}" >> "$LOG" 2>&1 || \
    echo "[$(date -Iseconds)] replaygain falló sobre algunos archivos" >> "$LOG"

# Lyrics: las trae el propio import (config lyrics.auto: yes). El paso
# standalone `beet lyrics` sin query barría TODA la librería (1600+) en cada
# corrida buscando lyrics faltantes — minutos de CPU en el N3060 + hits a
# LRCLib/Genius por archivos que nunca van a tener match. Eliminado 2026-07-09.

echo "[$(date -Iseconds)] beets-enrich done" >> "$LOG"
