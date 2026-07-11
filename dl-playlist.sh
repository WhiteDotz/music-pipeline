#!/bin/bash
# ============================================================================
# dl-playlist.sh - Descarga playlists de YouTube Music
# Uso: ./dl-playlist.sh "URL_DE_LA_PLAYLIST"
# ============================================================================

# Parsing de argumentos: soporta dos modos
#   1) dl-playlist.sh <URL_playlist> [nombre_override]
#   2) dl-playlist.sh --urls-file <archivo_tracks> <nombre_playlist>
#
# Flag opcional --no-m3u (cualquier posición): skip generación de playlist M3U
# al final. Usado por dl-single.sh para tracks sueltos que no forman playlist.
#
# --urls-file: archivo plano con líneas "<video_id>\t<title>\t<uploader>".
# Usado por gemini-explorer.sh para evitar requerir una URL de playlist real.
URLS_FILE=""
PLAYLIST_URL=""
PLAYLIST_NAME_OVERRIDE=""
NO_M3U=0

# Extract --no-m3u from anywhere in args before positional parsing
NEW_ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--no-m3u" ]; then
        NO_M3U=1
    else
        NEW_ARGS+=("$arg")
    fi
done
set -- "${NEW_ARGS[@]}"

if [ "${1:-}" = "--urls-file" ]; then
    URLS_FILE="$2"
    PLAYLIST_NAME_OVERRIDE="${3:-}"
    if [ -z "$URLS_FILE" ] || [ ! -f "$URLS_FILE" ]; then
        echo "❌ --urls-file requiere ruta a archivo existente" >&2
        exit 2
    fi
    if [ -z "$PLAYLIST_NAME_OVERRIDE" ]; then
        echo "❌ --urls-file requiere nombre playlist como tercer argumento" >&2
        exit 2
    fi
else
    PLAYLIST_URL="$1"
    PLAYLIST_NAME_OVERRIDE="${2:-}"  # opcional: nombre legible para M3U; si vacío, lo extrae de yt-dlp
fi
DOWNLOAD_DIR="$HOME/storage/Music"
PLAYLISTS_DIR="$DOWNLOAD_DIR/playlists"
COOKIES_FILE="$HOME/scripts/youtube-cookies.txt"
YTDLP="$HOME/scripts/.venv/bin/yt-dlp"
PYTHON="$HOME/scripts/.venv/bin/python3"
BUN="$HOME/.bun/bin/bun"
M3U_GEN="$HOME/scripts/playlist-m3u.py"
BEETS_ENRICH="$HOME/scripts/beets-enrich.sh"
START_EPOCH=$(date +%s)
# Bun es el runtime JS para resolver los n-challenges de YouTube.
# deno (default de yt-dlp) no está instalado; bun requiere declararse explícitamente.
# ejs:github descarga el solver la primera vez y lo cachea localmente.
YTDLP_JS_ARGS=(--no-js-runtimes --js-runtimes "bun:$BUN" --remote-components ejs:github)
ARCHIVE_FILE="$DOWNLOAD_DIR/.downloaded_archive.txt"
REDIRECT_CACHE="$DOWNLOAD_DIR/.redirect_cache.txt"
BLACKLIST_FILE="$DOWNLOAD_DIR/.blacklist.txt"
ARCHIVE_COUNT_FILE="$DOWNLOAD_DIR/.archive_count.txt"
CACHE_FILE="$DOWNLOAD_DIR/.playlist_cache.txt"
LOG_FILE="$DOWNLOAD_DIR/download.log"
MAX_WAIT_TIME=60
MAX_RETRIES=2

mkdir -p "$DOWNLOAD_DIR" "$PLAYLISTS_DIR"
touch "$LOG_FILE"

if [ ! -f "$COOKIES_FILE" ]; then
    echo "❌ No se encuentra el archivo de cookies: $COOKIES_FILE" | tee -a "$LOG_FILE"
    echo "   Ejecuta sync-cookies.sh para exportarlas desde la PC." | tee -a "$LOG_FILE"
    exit 1
fi

# --------------------------- LIMPIEZA INICIAL --------------------------------
echo "🧹 Eliminando archivos temporales..." | tee -a "$LOG_FILE"
find "$DOWNLOAD_DIR" -type f \( -name "*.temp.*" -o -name "*.opus" -size 0 \) -delete -print | tee -a "$LOG_FILE"
find "$DOWNLOAD_DIR" -maxdepth 1 -type f \( -name "*.webp" -o -name "*.png" -o -name "*.jpg" -o -name "*.opus.webp" -o -name "*.opus.png" -o -name "*.opus.jpg" \) -delete -print | tee -a "$LOG_FILE"

# ---------------------- CONTEO DE CANCIONES YA DESCARGADAS ------------------
total_descargadas=$(find "$DOWNLOAD_DIR" -maxdepth 1 -type f -name "*.opus" | wc -l)
echo "📀 Canciones ya descargadas (archivos .opus): $total_descargadas" | tee -a "$LOG_FILE"

# ---------------------- RECONSTRUCCIÓN DEL ARCHIVE ---------------------------
rebuild_archive() {
    echo "🔄 Reconstruyendo archive desde metadatos..." | tee -a "$LOG_FILE"
    "$PYTHON" "$HOME/scripts/rebuild-archive.py" "$DOWNLOAD_DIR" "$ARCHIVE_FILE" | tee -a "$LOG_FILE"
    echo "$total_descargadas" > "$ARCHIVE_COUNT_FILE"
}

touch "$REDIRECT_CACHE" "$BLACKLIST_FILE"

if [ -f "$ARCHIVE_FILE" ] && [ -f "$ARCHIVE_COUNT_FILE" ]; then
    last_count=$(cat "$ARCHIVE_COUNT_FILE")
    if [ "$last_count" -eq "$total_descargadas" ]; then
        echo "✅ Archive actualizado ($total_descargadas archivos)." | tee -a "$LOG_FILE"
    else
        echo "🔄 Nuevas descargas detectadas ($last_count → $total_descargadas). Reconstruyendo..." | tee -a "$LOG_FILE"
        rebuild_archive
    fi
else
    echo "🔄 Primera ejecución o falta archive." | tee -a "$LOG_FILE"
    rebuild_archive
fi

# Cargar archive + redirect cache + blacklist en memoria para búsquedas O(1)
declare -A archive_map
while IFS= read -r id; do
    [[ -n "$id" ]] && archive_map["$id"]=1
done < "$ARCHIVE_FILE"
while IFS= read -r id; do
    [[ -n "$id" ]] && archive_map["$id"]=1
done < "$REDIRECT_CACHE"

declare -A blacklist_map
while IFS= read -r id; do
    [[ -n "$id" ]] && blacklist_map["$id"]=1
done < "$BLACKLIST_FILE"

total_archive="${#archive_map[@]}"
echo "✅ Archive contiene $total_archive IDs únicos." | tee -a "$LOG_FILE"

# ------------------------- DATA SYNC ID (DESHABILITADO) ----------------------
# BUG histórico: get_data_sync_id() extraía el cookie APISID y lo pasaba como
# `--extractor-args youtube:data_sync_id=...`. Pero APISID NO es el data_sync_id
# (este último es el id de delegación de cuenta de innertube). Pasar un
# data_sync_id mismatcheado hace que YouTube minte el GVS PO token para un
# contexto de cuenta equivocado → "HTTP Error 403: Forbidden" al bajar el media
# (la extracción del player sí pasa; falla la descarga de datos). Rompía TODA
# descarga en lote (channels, grey, music). Diagnosticado 2026-06-13 con A/B:
# mismo video, mismo minuto, CON data_sync_id=APISID → 403; sin él → OK.
#
# Fix: NO pasar data_sync_id. yt-dlp lo auto-resuelve de las cookies cuando hace
# falta. El WARNING "Missing required Data Sync ID for tv_downgraded client" es
# inofensivo (degrada solo, descarga igual con player_client=web). Dejar
# DATA_SYNC_ID vacío → los 3 guards `[[ -n "$DATA_SYNC_ID" ]]` saltan el arg.
DATA_SYNC_ID=""

# ------------------------- OBTENER LISTA DE PLAYLIST ------------------------
# Descarga la lista UNA sola vez y calcula el hash desde los datos ya obtenidos.
TEMP_LIST="/tmp/playlist_$$.txt"

if [ -n "$URLS_FILE" ]; then
    # Modo --urls-file: copiar archivo a TEMP_LIST sin tocar yt-dlp.
    # Formato esperado: <video_id>\t<title>\t<uploader> por línea.
    echo "📥 Modo --urls-file: usando $URLS_FILE" | tee -a "$LOG_FILE"
    cp "$URLS_FILE" "$TEMP_LIST"
    PLAYLIST_NAME="$PLAYLIST_NAME_OVERRIDE"
else
    echo "🔄 Extrayendo lista desde YouTube Music..." | tee -a "$LOG_FILE"
    "$YTDLP" --cookies "$COOKIES_FILE" "${YTDLP_JS_ARGS[@]}" \
           --flat-playlist \
           --print "%(id)s	%(title)s	%(uploader|)s" \
           "$PLAYLIST_URL" > "$TEMP_LIST" 2>> "$LOG_FILE"
fi

# Nombre legible de la playlist (override opcional vía $2)
if [ -n "$PLAYLIST_NAME_OVERRIDE" ]; then
    PLAYLIST_NAME="$PLAYLIST_NAME_OVERRIDE"
else
    PLAYLIST_NAME=$("$YTDLP" --cookies "$COOKIES_FILE" "${YTDLP_JS_ARGS[@]}" \
        --flat-playlist --playlist-items 1 \
        --print "%(playlist_title|)s" \
        "$PLAYLIST_URL" 2>/dev/null | head -1)
fi
[ -z "$PLAYLIST_NAME" ] && PLAYLIST_NAME="playlist-$(date +%Y%m%d-%H%M)"
echo "🏷️  Nombre playlist: $PLAYLIST_NAME" | tee -a "$LOG_FILE"

if [ ! -s "$TEMP_LIST" ]; then
    echo "❌ Error: No se pudo obtener la lista." | tee -a "$LOG_FILE"
    exit 1
fi

PLAYLIST_TOTAL=$(wc -l < "$TEMP_LIST")

# Hash calculado desde los datos ya descargados (sin fetch adicional)
url_hash=$(echo -n "${PLAYLIST_URL:-$URLS_FILE}" | md5sum | cut -d' ' -f1)
ids_hash=$(awk -F'\t' '{print $1}' "$TEMP_LIST" | sort | md5sum | cut -d' ' -f1)
current_hash="$url_hash|$ids_hash"

if [ -f "$CACHE_FILE" ] && [ "$(head -n1 "$CACHE_FILE")" = "$current_hash" ]; then
    echo "✅ La playlist no ha cambiado. Caché válida." | tee -a "$LOG_FILE"
else
    echo "🔄 Playlist actualizada. Guardando nueva caché." | tee -a "$LOG_FILE"
    { echo "$current_hash"; echo "$PLAYLIST_TOTAL"; } > "$CACHE_FILE"
    cp "$TEMP_LIST" "$CACHE_FILE.list"
fi

echo "📋 Total canciones en la playlist: $PLAYLIST_TOTAL" | tee -a "$LOG_FILE"

echo "------------------------------------------"

# ---------------------------- FUNCIONES --------------------------------------
to_seconds() {
    echo "$1" | awk -F':' '{
        if (NF==2) print $1*60+$2;
        else if (NF==3) print $1*3600+$2*60+$3;
        else print $1
    }'
}

# Resolver redirección usando yt-dlp con cookies (curl sin auth no ve el redirect real)
resolve_video_id() {
    local video_id="$1"
    local resolved
    resolved=$("$YTDLP" --cookies "$COOKIES_FILE" "${YTDLP_JS_ARGS[@]}" \
        --no-warnings \
        --print "%(id)s" \
        --skip-download \
        "https://music.youtube.com/watch?v=$video_id" 2>/dev/null | head -1)

    if [[ -n "$resolved" && "$resolved" != "$video_id" ]]; then
        echo "$resolved"
        echo "   [DEBUG] Resuelto: $video_id -> $resolved" >> "$LOG_FILE"
        return 0
    fi

    echo "$video_id"
    return 1
}

# Buscar alternativa en YouTube Music por título (+ uploader si está disponible)
search_alternative() {
    local title="$1"
    local uploader="$2"
    local query="$title"
    [[ -n "$uploader" ]] && query="$title $uploader"

    "$YTDLP" --cookies "$COOKIES_FILE" "${YTDLP_JS_ARGS[@]}" \
        --no-warnings \
        --flat-playlist \
        --print "%(id)s	%(title)s	%(uploader)s" \
        "ytsearch3:$query" 2>/dev/null | head -1
}

# Obtener mejor formato de audio en una sola llamada a yt-dlp
get_best_audio_format() {
    local video_id="$1"
    local -a cmd=("$YTDLP" --cookies "$COOKIES_FILE" "${YTDLP_JS_ARGS[@]}"
        --extractor-args "youtube:player_client=web"
        --list-formats --no-warnings
        "https://music.youtube.com/watch?v=$video_id")
    [[ -n "$DATA_SYNC_ID" ]] && cmd+=(--extractor-args "youtube:data_sync_id=$DATA_SYNC_ID")

    local formats
    formats=$("${cmd[@]}" 2>/dev/null)

    for f in 251 250 140; do
        if echo "$formats" | grep -q "^$f"; then
            echo "$f"
            return
        fi
    done

    local fallback
    fallback=$(echo "$formats" | grep -E '^\s*[0-9]+\s+audio only' | awk '{print $1}' | head -1)
    echo "${fallback:-bestaudio}"
}

# Descargar video
download_video() {
    local video_id="$1"
    local output_file="$2"
    local archive_file="$3"
    local format="$4"
    local log_file="$5"

    local -a cmd=("$YTDLP" "${YTDLP_JS_ARGS[@]}"
        -f "$format"
        --remux-video opus
        --embed-thumbnail
        --embed-metadata
        --no-write-thumbnail
        --convert-thumbnails jpg
        --ppa "ThumbnailsConvertor+ffmpeg_o:-vf crop=ih:ih"
        # Solo el artista PRINCIPAL en el tag ARTIST (artists.0). Sin esto,
        # yt-dlp embebe todos los colaboradores unidos por coma ("A, B, C")
        # y Poweramp/Navidrome muestran esa string como un artista único.
        # Si el video no trae lista artists (no-music), el parse falla
        # silencioso y queda el artist default.
        --parse-metadata "%(artists.0)s:%(meta_artist)s"
        --download-archive "$archive_file"
        -o "$output_file"
        --remote-components ejs:github
        --cookies "$COOKIES_FILE"
        --user-agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        --age-limit 99
        --extractor-args "youtube:player_client=web"
        --extractor-args "youtube:player_skip=webpage"
        -r 3M)
    [[ -n "$DATA_SYNC_ID" ]] && cmd+=(--extractor-args "youtube:data_sync_id=$DATA_SYNC_ID")
    cmd+=("https://music.youtube.com/watch?v=$video_id")

    local tmp_err="/tmp/ytdlp_err_$$.txt"
    "${cmd[@]}" >> "$log_file" 2>"$tmp_err"
    local exit_code=$?
    cat "$tmp_err" >> "$log_file"

    if grep -qi "Video unavailable\|This video is not available\|has been removed\|video is private" "$tmp_err" 2>/dev/null; then
        rm -f "$tmp_err"
        return 2  # no disponible — candidato a blacklist
    fi
    rm -f "$tmp_err"
    return $exit_code
}

# ---------------------------- BUCLE PRINCIPAL --------------------------------
contador_nuevas=0
contador_ok=0
contador_existentes=0
contador_errores=0

while IFS=$'\t' read -r video_id title uploader; do
    clean_title=$(echo "$title" | sed -e 's/^[[:space:]-]*//' -e 's/[\/:*?"<>|]/-/g' -e 's/^-//')

    # Manejo de nombres duplicados
    final_name="$DOWNLOAD_DIR/$clean_title.opus"
    counter=1
    while [ -f "$final_name" ]; do
        final_name="$DOWNLOAD_DIR/${clean_title}(${counter}).opus"
        counter=$((counter + 1))
    done

    # Verificar archive — O(1)
    if [[ -n "${archive_map[$video_id]}" ]]; then
        echo "[EXISTE] $clean_title ($video_id)" >> "$LOG_FILE"
        contador_existentes=$((contador_existentes + 1))
        continue
    fi

    # Verificar blacklist
    if [[ -n "${blacklist_map[$video_id]}" ]]; then
        echo "[BLACKLIST] $clean_title ($video_id)" >> "$LOG_FILE"
        contador_errores=$((contador_errores + 1))
        continue
    fi

    if [[ "$title" == *"Private video"* ]]; then
        echo "[PRIVADO] $clean_title ($video_id)" | tee -a "$LOG_FILE"
        contador_errores=$((contador_errores + 1))
        continue
    fi

    # Resolver posible redirección
    echo "   🔍 $clean_title: resolviendo posible redirección..." | tee -a "$LOG_FILE"
    playlist_id="$video_id"
    real_id=$(resolve_video_id "$video_id")
    if [ "$real_id" != "$video_id" ]; then
        echo "   🔄 Redirección detectada: $video_id -> $real_id" | tee -a "$LOG_FILE"
        video_id="$real_id"
        if [[ -n "${archive_map[$video_id]}" ]]; then
            echo "$playlist_id" >> "$REDIRECT_CACHE"
            archive_map["$playlist_id"]=1
            echo "[EXISTE tras redir] $clean_title ($video_id)" | tee -a "$LOG_FILE"
            contador_existentes=$((contador_existentes + 1))
            continue
        fi
    fi

    # Obtener formato de audio
    audio_format=$(get_best_audio_format "$video_id")
    echo "[NUEVO] $clean_title ($video_id) - formato $audio_format" | tee -a "$LOG_FILE"
    contador_nuevas=$((contador_nuevas + 1))

    # Duración y pausa
    local_cmd=("$YTDLP" --cookies "$COOKIES_FILE" "${YTDLP_JS_ARGS[@]}"
        --extractor-args "youtube:player_client=web"
        --get-duration --no-warnings
        "https://music.youtube.com/watch?v=$video_id")
    [[ -n "$DATA_SYNC_ID" ]] && local_cmd+=(--extractor-args "youtube:data_sync_id=$DATA_SYNC_ID")
    duration_str=$("${local_cmd[@]}" 2>/dev/null)

    if [[ -z "$duration_str" ]]; then
        wait_time=$MAX_WAIT_TIME
    else
        duration_sec=$(to_seconds "$duration_str")
        wait_time=$(( duration_sec < MAX_WAIT_TIME ? duration_sec : MAX_WAIT_TIME ))
    fi
    random_extra=$((RANDOM % 15))
    total_wait=$((wait_time + random_extra))
    echo "   ⏱ Espera: ${total_wait}s (canción ${duration_str:-?})" >> "$LOG_FILE"

    success=0
    unavailable=0
    for attempt in $(seq 1 $MAX_RETRIES); do
        echo "   Intento $attempt..." >> "$LOG_FILE"
        download_video "$video_id" "$final_name" "$ARCHIVE_FILE" "$audio_format" "$LOG_FILE"
        dl_result=$?
        if [ $dl_result -eq 0 ]; then
            success=1; break
        elif [ $dl_result -eq 2 ]; then
            unavailable=1; break  # no reintentar
        fi
        sleep $((10 * attempt))
    done

    if [ $success -eq 1 ]; then
        echo "[OK] $clean_title ($video_id)" | tee -a "$LOG_FILE"
        contador_ok=$((contador_ok + 1))
        echo "$video_id" >> "$ARCHIVE_FILE"
        archive_map["$video_id"]=1
        total_descargadas=$((total_descargadas + 1))
        echo "$total_descargadas" > "$ARCHIVE_COUNT_FILE"
        sleep "$total_wait"
    elif [ $unavailable -eq 1 ]; then
        echo "[NO DISPONIBLE] $clean_title ($video_id) → blacklist" | tee -a "$LOG_FILE"
        echo "$video_id" >> "$BLACKLIST_FILE"
        blacklist_map["$video_id"]=1
        contador_errores=$((contador_errores + 1))
        rm -f "$DOWNLOAD_DIR"/*"$video_id"*.webp "$DOWNLOAD_DIR"/*"$video_id"*.jpg 2>/dev/null

        # Buscar alternativa por título + uploader
        echo "   🔎 Buscando alternativa: \"$clean_title\"${uploader:+ ($uploader)}..." | tee -a "$LOG_FILE"
        alt_result=$(search_alternative "$clean_title" "$uploader")
        if [[ -n "$alt_result" ]]; then
            alt_id=$(awk -F'\t' '{print $1}' <<< "$alt_result")
            alt_title=$(awk -F'\t' '{print $2}' <<< "$alt_result")
            alt_uploader=$(awk -F'\t' '{print $3}' <<< "$alt_result")
            echo "   🎵 Alternativa: $alt_title - $alt_uploader ($alt_id)" | tee -a "$LOG_FILE"

            if [[ -n "${archive_map[$alt_id]}" ]]; then
                echo "   ✅ Alternativa ya descargada ($alt_id). Nada que hacer." | tee -a "$LOG_FILE"
                contador_errores=$((contador_errores - 1))
            elif [[ -n "${blacklist_map[$alt_id]}" ]]; then
                echo "   ⚠️ Alternativa también no disponible ($alt_id). Saltando." | tee -a "$LOG_FILE"
            else
                alt_format=$(get_best_audio_format "$alt_id")
                alt_success=0
                for attempt in $(seq 1 $MAX_RETRIES); do
                    download_video "$alt_id" "$final_name" "$ARCHIVE_FILE" "$alt_format" "$LOG_FILE"
                    [ $? -eq 0 ] && { alt_success=1; break; }
                    sleep $((10 * attempt))
                done
                if [ $alt_success -eq 1 ]; then
                    echo "[OK-ALT] $clean_title → $alt_title ($alt_id)" | tee -a "$LOG_FILE"
                    contador_ok=$((contador_ok + 1))
                    echo "$alt_id" >> "$ARCHIVE_FILE"
                    archive_map["$alt_id"]=1
                    total_descargadas=$((total_descargadas + 1))
                    echo "$total_descargadas" > "$ARCHIVE_COUNT_FILE"
                    contador_errores=$((contador_errores - 1))
                    sleep "$total_wait"
                else
                    echo "[ERROR-ALT] No se pudo descargar la alternativa." | tee -a "$LOG_FILE"
                fi
            fi
        else
            echo "   ❌ Sin alternativa encontrada para: $clean_title" | tee -a "$LOG_FILE"
        fi
    else
        echo "[ERROR] $clean_title ($video_id)" | tee -a "$LOG_FILE"
        contador_errores=$((contador_errores + 1))
        rm -f "$DOWNLOAD_DIR"/*"$video_id"*.webp "$DOWNLOAD_DIR"/*"$video_id"*.jpg 2>/dev/null
    fi
done < "$TEMP_LIST"

rm -f /tmp/debug_*.html 2>/dev/null

echo "------------------------------------------"
echo "🎉 Proceso finalizado." | tee -a "$LOG_FILE"
echo "   ✅ Existentes: $contador_existentes" | tee -a "$LOG_FILE"
echo "   🆕 Nuevas descargadas: $contador_ok" | tee -a "$LOG_FILE"
echo "   ❌ Errores: $contador_errores" | tee -a "$LOG_FILE"
echo "   📦 Total en archive: $(wc -l < "$ARCHIVE_FILE")" | tee -a "$LOG_FILE"
echo "   💿 Total archivos .opus: $total_descargadas" | tee -a "$LOG_FILE"

# ----------------------- POST-DOWNLOAD HOOKS --------------------------------

# 1. Beets enrichment sobre archivos nuevos (mtime > inicio del script)
if [ -x "$BEETS_ENRICH" ]; then
    echo "🎼 Ejecutando enrichment beets sobre archivos nuevos..." | tee -a "$LOG_FILE"
    "$BEETS_ENRICH" --since "$START_EPOCH" >> "$LOG_FILE" 2>&1 || \
        echo "   ⚠️ beets-enrich.sh terminó con warnings (ver beets.log)" | tee -a "$LOG_FILE"
else
    echo "ℹ️ beets-enrich.sh no está disponible — saltando enrichment" | tee -a "$LOG_FILE"
fi

# 2. Generar M3U para Navidrome (siempre, aunque no haya descargas nuevas)
if [ "$NO_M3U" -eq 1 ]; then
    echo "ℹ️ --no-m3u: saltando generación M3U" | tee -a "$LOG_FILE"
elif [ -x "$M3U_GEN" ] || [ -f "$M3U_GEN" ]; then
    echo "📝 Generando M3U para playlist..." | tee -a "$LOG_FILE"
    "$PYTHON" "$M3U_GEN" \
        --music-dir "$DOWNLOAD_DIR" \
        --ids-file "$TEMP_LIST" \
        --name "$PLAYLIST_NAME" 2>&1 | tee -a "$LOG_FILE"
else
    echo "ℹ️ playlist-m3u.py no encontrado — saltando M3U" | tee -a "$LOG_FILE"
fi

rm -f "$TEMP_LIST" 2>/dev/null
