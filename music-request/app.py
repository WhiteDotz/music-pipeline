#!/usr/bin/env python3
"""music-request — mini web para pedir música por búsqueda o URL.

Flujo:
  1. Buscás (ytmusicapi, filtro songs) o pegás una URL → elegís resultado.
  2. Worker (thread único) descarga vía dl-playlist.sh --urls-file --no-m3u
     (mismo pipeline: archive dedup, beets enrich, artista principal).
  3. Al terminar agrega el track a ~/storage/Music/.pedidos_ids.txt y regenera
     playlists/pedidos.m3u (#PLAYLIST:Pedidos, nombre estable) con
     playlist-m3u.py → aparece en Navidrome/Feishin y, vía Syncthing, en
     Poweramp. Dispara scan de Navidrome.

Si el track ya está en biblioteca, dl-playlist lo skipea (rápido) y solo se
agrega a Pedidos — sirve también para "encolar" algo que ya tenés.

Puerto 5004, bind 0.0.0.0 (LAN + tailnet, sin auth — igual que el monitor
:5003). Servicio: music-request.service (systemd --user).
"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

HOME = Path.home()
MUSIC_DIR = HOME / "storage/Music"
PEDIDOS_IDS = MUSIC_DIR / ".pedidos_ids.txt"
DL_PLAYLIST = HOME / "scripts/dl-playlist.sh"
M3U_GEN = HOME / "scripts/playlist-m3u.py"
PYTHON = HOME / "scripts/.venv/bin/python3"
YTDLP = HOME / "scripts/.venv/bin/yt-dlp"
BUN = HOME / ".bun/bin/bun"
COOKIES = HOME / "scripts/youtube-cookies.txt"
ENV_FILE = HOME / ".lb-sync.env"
PLAYLIST_NAME = "Pedidos"
PORT = 5004

app = Flask(__name__)
jobs: dict[str, dict] = {}          # job_id → estado (memoria; se pierde al restart)
job_queue: "queue.Queue[str]" = queue.Queue()
_yt = None
_lock = threading.Lock()


def ytmusic():
    global _yt
    if _yt is None:
        from ytmusicapi import YTMusic
        _yt = YTMusic()
    return _yt


def resolve_url(url: str) -> tuple[str, str, str] | None:
    """URL → (video_id, title, uploader) vía yt-dlp (protege contra &list=)."""
    try:
        out = subprocess.run(
            [str(YTDLP), "--cookies", str(COOKIES),
             "--no-js-runtimes", "--js-runtimes", f"bun:{BUN}",
             "--remote-components", "ejs:github",
             "--no-warnings", "--flat-playlist", "--playlist-items", "1",
             "--print", "%(id)s\t%(title)s\t%(uploader|)s", url],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip().splitlines()
    except subprocess.TimeoutExpired:
        return None
    if not out:
        return None
    parts = out[0].split("\t")
    return (parts[0], parts[1] if len(parts) > 1 else "?",
            parts[2] if len(parts) > 2 else "")


def navidrome_scan() -> None:
    try:
        env = dict(l.split("=", 1) for l in ENV_FILE.read_text().splitlines()
                   if "=" in l and not l.startswith("#"))
        salt = secrets.token_hex(8)
        q = urllib.parse.urlencode({
            "u": env["NAVIDROME_USER"],
            "t": hashlib.md5((env["NAVIDROME_PASS"] + salt).encode()).hexdigest(),
            "s": salt, "v": "1.16.1", "c": "music-request", "f": "json"})
        urllib.request.urlopen(
            f"{env.get('NAVIDROME_BASE', 'http://127.0.0.1:4533')}/rest/startScan?{q}",
            timeout=15).read()
    except Exception:
        pass  # scan es best-effort; el watcher de Navidrome igual lo agarra


def append_pedido(vid: str, title: str, artist: str) -> None:
    """Agrega al historial de pedidos (dedup) y regenera pedidos.m3u."""
    with _lock:
        lines = PEDIDOS_IDS.read_text().splitlines() if PEDIDOS_IDS.exists() else []
        if not any(l.split("\t", 1)[0] == vid for l in lines):
            lines.append(f"{vid}\t{title}\t{artist}")
            PEDIDOS_IDS.write_text("\n".join(lines) + "\n")
        subprocess.run(
            [str(PYTHON), str(M3U_GEN),
             "--music-dir", str(MUSIC_DIR),
             "--ids-file", str(PEDIDOS_IDS),
             "--name", PLAYLIST_NAME],
            capture_output=True, text=True, timeout=600,
        )


def worker() -> None:
    while True:
        job_id = job_queue.get()
        job = jobs[job_id]
        job["status"] = "descargando"
        tsv = Path(f"/tmp/music-request-{job_id}.tsv")
        tsv.write_text(f"{job['vid']}\t{job['title']}\t{job['artist']}\n")
        try:
            rc = subprocess.run(
                [str(DL_PLAYLIST), "--urls-file", str(tsv), "_pedido", "--no-m3u"],
                capture_output=True, text=True, timeout=900,
            )
            # Éxito = el vid quedó en el archive (o ya estaba / redirect).
            # No sirve grepear stdout: "[EXISTE]" va solo al download.log.
            if rc.returncode == 0 and job["vid"] in known_vids():
                append_pedido(job["vid"], job["title"], job["artist"])
                navidrome_scan()
                job["status"] = "listo"
            else:
                tail = (rc.stdout + rc.stderr)[-300:]
                job["status"] = "error"
                job["detail"] = tail
        except subprocess.TimeoutExpired:
            job["status"] = "error"
            job["detail"] = "timeout (15 min)"
        except Exception as e:
            job["status"] = "error"
            job["detail"] = str(e)
        finally:
            tsv.unlink(missing_ok=True)
            job["done_at"] = time.time()
            job_queue.task_done()


PAGE = """<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pedidos de música — hp15</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background: #111418; color: #e8e6e3;
         max-width: 640px; margin: 0 auto; padding: 1rem; }
  h1 { font-size: 1.2rem; } h1 span { color: #7aa2f7; }
  form { display: flex; gap: .5rem; }
  input[type=text] { flex: 1; padding: .6rem .8rem; border-radius: 8px;
         border: 1px solid #333; background: #1a1e24; color: inherit; font-size: 1rem; }
  button { padding: .6rem 1rem; border-radius: 8px; border: 0;
         background: #7aa2f7; color: #111; font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .5; }
  button.sec { background: #2a2f3a; color: #c8ccd4; font-weight: 500;
         padding: .45rem .7rem; font-size: .85rem; }
  #ft { display: flex; gap: 1rem; margin: .5rem 0 0; color: #9aa0a6; font-size: .9rem; }
  #ft label { cursor: pointer; }
  .result, .job { display: flex; gap: .7rem; align-items: center;
         background: #1a1e24; border-radius: 10px; padding: .6rem .8rem; margin: .5rem 0; }
  .result img { width: 44px; height: 44px; border-radius: 6px; object-fit: cover; }
  .result img.round { border-radius: 50%; }
  .meta { flex: 1; min-width: 0; }
  .meta b, .meta small { display: block; overflow: hidden; text-overflow: ellipsis;
         white-space: nowrap; }
  small { color: #9aa0a6; }
  .lib { color: #9ece6a; }
  .st-listo { color: #9ece6a; } .st-error { color: #f7768e; }
  .st-descargando, .st-en-cola { color: #e0af68; }
  #status { min-height: 1.4rem; color: #9aa0a6; }
</style></head><body>
<h1>🎵 Pedidos de música <span>hp15</span></h1>
<form id="f">
  <input type="text" id="q" placeholder="Canción, artista… o URL" autofocus>
  <button type="submit">Buscar</button>
</form>
<div id="ft">
  <label><input type="radio" name="ft" value="songs" checked> Canciones</label>
  <label><input type="radio" name="ft" value="artists"> Artistas</label>
</div>
<div id="status"></div>
<div id="results"></div>
<h2 style="font-size:1rem">Cola / recientes</h2>
<div id="jobs"><small>vacío</small></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; };
const qs = s => esc(s).replace(/"/g, '&quot;');

function songRow(t) {
  return `
  <div class="result" data-vid="${qs(t.vid)}" data-title="${qs(t.title)}"
       data-artist="${qs(t.artist)}" data-aid="${qs(t.aid || '')}">
    ${t.thumb ? `<img src="${qs(t.thumb)}" alt="">` : ''}
    <div class="meta"><b>${esc(t.title)}</b>
      <small>${esc(t.artist)}${t.album ? ' · ' + esc(t.album) : ''}${t.duration ? ' · ' + esc(t.duration) : ''}${t.in_lib ? ' · <span class="lib">✓ en biblioteca</span>' : ''}</small>
    </div>
    ${t.aid ? `<button class="sec btn-more" title="Más canciones de este artista">más</button>` : ''}
    <button class="btn-add" title="Pedir">＋</button>
  </div>`;
}

function artistRow(a) {
  return `
  <div class="result" data-aid="${qs(a.aid)}" data-name="${qs(a.name)}">
    ${a.thumb ? `<img class="round" src="${qs(a.thumb)}" alt="">` : ''}
    <div class="meta"><b>${esc(a.name)}</b><small>artista</small></div>
    <button class="sec btn-artist">ver canciones ▸</button>
  </div>`;
}

$('f').addEventListener('submit', async e => {
  e.preventDefault();
  const q = $('q').value.trim();
  if (!q) return;
  const flt = document.querySelector('input[name=ft]:checked').value;
  $('status').textContent = 'Buscando…';
  $('results').innerHTML = '';
  const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&filter=' + flt);
  const data = await r.json();
  $('status').textContent = data.error ? ('Error: ' + data.error)
      : (data.results.length ? '' : 'Sin resultados');
  $('results').innerHTML = data.results
      .map(data.kind === 'artists' ? artistRow : songRow).join('');
});

$('results').addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const d = btn.closest('.result').dataset;
  if (btn.classList.contains('btn-add')) pedir(d.vid, d.title, d.artist, btn);
  else if (btn.classList.contains('btn-more')) loadArtist(d.aid, d.artist);
  else if (btn.classList.contains('btn-artist')) loadArtist(d.aid, d.name);
});

async function loadArtist(aid, name) {
  $('status').textContent = 'Cargando canciones de ' + name + '…';
  $('results').innerHTML = '';
  const r = await fetch('/api/artist?id=' + encodeURIComponent(aid));
  const data = await r.json();
  if (data.error) { $('status').textContent = 'Error: ' + data.error; return; }
  $('status').textContent = 'Canciones de ' + (data.name || name) +
      ' (' + data.results.length + ')';
  $('results').innerHTML = data.results.map(songRow).join('');
}

async function pedir(vid, title, artist, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  await fetch('/api/request', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({vid, title, artist})
  });
  if (btn) btn.textContent = '✓';
  loadJobs();
}

async function loadJobs() {
  const r = await fetch('/api/jobs');
  const data = await r.json();
  $('jobs').innerHTML = data.jobs.length ? data.jobs.map(j => `
    <div class="job"><div class="meta"><b>${esc(j.title)}</b>
      <small>${esc(j.artist)}</small></div>
      <span class="st-${j.status.replace(' ', '-')}">${esc(j.status)}</span>
    </div>`).join('') : '<small>vacío</small>';
  if (data.jobs.some(j => ['en cola', 'descargando'].includes(j.status)))
    setTimeout(loadJobs, 3000);
}
loadJobs();
</script></body></html>"""


@app.get("/")
def index():
    return render_template_string(PAGE)


_known_cache: dict = {"mtimes": None, "vids": set()}


def known_vids() -> set[str]:
    """IDs ya en biblioteca (archive ∪ redirect cache), cache por mtime."""
    files = [MUSIC_DIR / ".downloaded_archive.txt",
             MUSIC_DIR / ".redirect_cache.txt"]
    mt = tuple(f.stat().st_mtime if f.exists() else 0 for f in files)
    if _known_cache["mtimes"] != mt:
        vids: set[str] = set()
        for f in files:
            if f.exists():
                for line in f.read_text().splitlines():
                    for tok in line.split():
                        if re.fullmatch(r"[\w-]{11}", tok):
                            vids.add(tok)
        _known_cache["mtimes"] = mt
        _known_cache["vids"] = vids
    return _known_cache["vids"]


BEETS_DB = HOME / "scripts/beets/library.db"
_lib_cache: dict = {"mtime": None, "keys": set()}


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", (s or "").casefold())


def lib_keys() -> set[str]:
    """Claves título|artista de la biblioteca (beets DB), cache por mtime.

    Capa 2 de "en biblioteca": YTM devuelve videoIds de versión álbum que no
    coinciden con el id descargado (video oficial) — el vid solo no alcanza."""
    if not BEETS_DB.exists():
        return set()
    mt = BEETS_DB.stat().st_mtime
    if _lib_cache["mtime"] != mt:
        import sqlite3
        keys: set[str] = set()
        try:
            con = sqlite3.connect(f"file:{BEETS_DB}?mode=ro", uri=True)
            for title, artist in con.execute("SELECT title, artist FROM items"):
                if title and artist:
                    keys.add(_norm(title) + "|" + _norm(artist))
            con.close()
            _lib_cache["mtime"] = mt
            _lib_cache["keys"] = keys
        except Exception:
            return _lib_cache["keys"]
    return _lib_cache["keys"]


def in_library(vid: str, title: str, artist: str) -> bool:
    if vid in known_vids():
        return True
    return bool(title and artist
                and _norm(title) + "|" + _norm(artist) in lib_keys())


def song_result(h: dict, fallback_artist: str = "") -> dict | None:
    """Hit de ytmusicapi (search songs / get_playlist track) → dict para la UI.

    aid = browseId del artista principal (para el botón "más")."""
    vid = h.get("videoId")
    if not vid:
        return None
    arts = h.get("artists") or []
    thumbs = h.get("thumbnails") or []
    album = h.get("album")
    title = h.get("title") or "?"
    primary = (arts[0].get("name", "") if arts else "") or fallback_artist
    return {
        "vid": vid,
        "title": title,
        "artist": ", ".join(a.get("name", "") for a in arts) or fallback_artist,
        "aid": next((a.get("id") for a in arts if a.get("id")), "") or "",
        "album": album.get("name", "") if isinstance(album, dict) else "",
        "duration": h.get("duration") or "",
        "thumb": thumbs[0]["url"] if thumbs else "",
        "in_lib": in_library(vid, title, primary),
    }


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    flt = request.args.get("filter", "songs")
    if not q:
        return jsonify({"results": []})
    if re.match(r"^https?://", q):
        r = resolve_url(q)
        if not r:
            return jsonify({"results": [], "error": "no pude resolver la URL"})
        vid, title, artist = r
        return jsonify({"results": [{"vid": vid, "title": title, "artist": artist,
                                     "aid": "", "album": "", "duration": "",
                                     "thumb": "",
                                     "in_lib": in_library(vid, title, artist)}]})
    if flt == "artists":
        try:
            hits = ytmusic().search(q, filter="artists", limit=8)
        except Exception as e:
            return jsonify({"results": [], "error": str(e)[:200]})
        results = []
        for h in hits[:8]:
            bid = h.get("browseId")
            if not bid:
                continue
            thumbs = h.get("thumbnails") or []
            results.append({"aid": bid,
                            "name": h.get("artist") or h.get("name") or "?",
                            "thumb": thumbs[0]["url"] if thumbs else ""})
        return jsonify({"kind": "artists", "results": results})
    try:
        hits = ytmusic().search(q, filter="songs", limit=8)
    except Exception as e:
        return jsonify({"results": [], "error": str(e)[:200]})
    results = [r for r in (song_result(h) for h in hits[:8]) if r]
    return jsonify({"kind": "songs", "results": results})


@app.get("/api/artist")
def api_artist():
    """Top canciones de un artista (browseId). Intenta la playlist completa de
    "Songs" del canal (hasta 50); si no hay, cae a los ~5 del perfil."""
    aid = (request.args.get("id") or "").strip()
    if not re.fullmatch(r"[\w-]{1,64}", aid):
        return jsonify({"results": [], "error": "id inválido"}), 400
    try:
        artist = ytmusic().get_artist(aid)
    except Exception as e:
        return jsonify({"results": [], "error": str(e)[:200]})
    name = artist.get("name") or "?"
    songs = artist.get("songs") or {}
    tracks = []
    pl = songs.get("browseId")
    if pl:
        try:
            tracks = ytmusic().get_playlist(pl, limit=50).get("tracks") or []
        except Exception:
            tracks = []
    if not tracks:
        tracks = songs.get("results") or []
    results = [r for r in (song_result(t, fallback_artist=name)
                           for t in tracks[:50]) if r]
    return jsonify({"name": name, "results": results})


@app.post("/api/request")
def api_request():
    data = request.get_json(silent=True) or {}
    vid = (data.get("vid") or "").strip()
    if not re.fullmatch(r"[\w-]{11}", vid):
        return jsonify({"error": "videoId inválido"}), 400
    # dedup: ya en cola o bajando
    for j in jobs.values():
        if j["vid"] == vid and j["status"] in ("en cola", "descargando"):
            return jsonify({"job": None, "note": "ya en cola"})
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id, "vid": vid,
        "title": (data.get("title") or "?")[:200],
        "artist": (data.get("artist") or "")[:200],
        "status": "en cola", "created_at": time.time(),
    }
    job_queue.put(job_id)
    return jsonify({"job": job_id})


@app.get("/api/jobs")
def api_jobs():
    recent = sorted(jobs.values(), key=lambda j: -j["created_at"])[:20]
    return jsonify({"jobs": recent})


threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
