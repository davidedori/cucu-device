from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List, Dict
from pathlib import Path
from datetime import datetime
from dotenv import dotenv_values
import hashlib
import hmac
import json
import os
import secrets
import re
import shutil
import subprocess
import time
import socket
import threading
import urllib.request
import urllib.error

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg"}

API_DIR = Path(__file__).resolve().parent

app = FastAPI()

# Percorsi base (stessi del tuo script NFC). Override via BASE_DIR per testare
# l'API senza toccare i dati reali del dispositivo (vedi CLAUDE.md, sezione test).
BASE_DIR = Path(os.environ.get("BASE_DIR") or API_DIR.parent)
CHARACTERS_DIR = BASE_DIR / "characters"
EPISODE_STATE_FILE = BASE_DIR / "episode_state.json"
TAGS_FILE = BASE_DIR / "tags.json"
CONFIG_ENV_FILE = BASE_DIR / "config.env"
VERSION_FILE = BASE_DIR / "VERSION"
LAST_SEEN_TAG_FILE = BASE_DIR / "last_seen_tag.json"
TIME_LIMITS_FILE = BASE_DIR / "time_limits.json"
TAG_LABELS_FILE = BASE_DIR / "tag_labels.json"
UI_AUTH_FILE = BASE_DIR / "ui_auth.json"
DAILY_USAGE_FILE = BASE_DIR / "daily_usage.json"

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Font e immagini della UI serviti in locale: niente CDN, così la pagina
# funziona anche quando il telefono è collegato all'hotspot del Cucù (senza internet)
app.mount("/static", StaticFiles(directory=API_DIR / "static"), name="static")

class CharacterCreate(BaseModel):
    name: str
    display_name: Optional[str] = None

class TagCreate(BaseModel):
    uid: str
    # True: se la statuina è già di un altro personaggio, la sposta qui
    move: bool = False

class TagLabel(BaseModel):
    label: str = ""

class EpisodeRename(BaseModel):
    new_filename: str

class TimeWindow(BaseModel):
    start: str
    end: str

class DayLimitConfig(BaseModel):
    daily_limit_minutes: Optional[int] = None
    windows: List[TimeWindow] = []

class TimeLimitsConfig(BaseModel):
    enabled: bool = False
    days: Dict[str, DayLimitConfig] = {}
    exempt_characters: List[str] = []

class TimeLimitExempt(BaseModel):
    exempt: bool

def _write_json_atomic(path: Path, data):
    """Scrive su un file temporaneo e lo rinomina: chi legge (read_nfc.py) non
    vede mai un file a metà, e una mancanza di corrente lascia il file vecchio."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def load_episode_state():
    if EPISODE_STATE_FILE.exists():
        try:
            with EPISODE_STATE_FILE.open() as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore nel leggere {EPISODE_STATE_FILE}: {e}")
            return {}
    return {}

def save_episode_state(state: dict):
    try:
        _write_json_atomic(EPISODE_STATE_FILE, state)
    except Exception as e:
        print(f"Errore nel salvare {EPISODE_STATE_FILE}: {e}")


def load_tags():
    if TAGS_FILE.exists():
        try:
            with TAGS_FILE.open() as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore nel leggere {TAGS_FILE}: {e}")
            return {}
    return {}

def save_tags(tags: dict):
    try:
        _write_json_atomic(TAGS_FILE, tags)
    except Exception as e:
        print(f"Errore nel salvare {TAGS_FILE}: {e}")


def _norm_uid(uid: str) -> str:
    """UID nel formato dei lettori: byte esadecimali minuscoli separati da uno spazio."""
    return " ".join(uid.strip().split()).lower()

def _find_uid(tags_map: dict, uid: str):
    """Chiave di tags.json che corrisponde a uid ignorando maiuscole e spazi
    (le voci inserite a mano in passato potevano essere in maiuscolo)."""
    target = _norm_uid(uid)
    for key in tags_map:
        if _norm_uid(key) == target:
            return key
    return None

def _display_name(episode_state: dict, name: str) -> str:
    return episode_state.get(name, {}).get("display_name", name.replace("_", " ").title())

def load_tag_labels():
    if TAG_LABELS_FILE.exists():
        try:
            with TAG_LABELS_FILE.open() as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore nel leggere {TAG_LABELS_FILE}: {e}")
    return {}

def save_tag_labels(labels: dict):
    try:
        _write_json_atomic(TAG_LABELS_FILE, labels)
    except Exception as e:
        print(f"Errore nel salvare {TAG_LABELS_FILE}: {e}")

# --- Anteprime degli episodi -----------------------------------------------------
# Un fotogramma per episodio, creato con ffmpeg alla prima richiesta e salvato in
# characters/<nome>/.thumbs/ (read_nfc.py e l'API ignorano le cartelle: contano
# solo i file video). Sul Pi Zero si genera una sola anteprima alla volta, a
# bassa priorità, e mai mentre un episodio è in riproduzione.

THUMB_DIR_NAME = ".thumbs"
THUMB_WIDTH = 320
_thumb_lock = threading.Lock()

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None

def _thumb_path(char_dir: Path, filename: str) -> Path:
    return char_dir / THUMB_DIR_NAME / f"{filename}.jpg"

def _thumb_version(char_dir: Path, video: Path):
    """mtime dell'anteprima se è ancora valida (più recente del video), altrimenti None."""
    thumb = _thumb_path(char_dir, video.name)
    try:
        t = thumb.stat().st_mtime
        return int(t) if t >= video.stat().st_mtime else None
    except OSError:
        return None

def _video_duration(path: Path):
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return float(res.stdout.strip())
    except Exception:
        return None

def _generate_thumb(char_dir: Path, video: Path) -> bool:
    """Estrae un fotogramma (circa al 30% del video, massimo 90 s: dopo la sigla)."""
    thumb = _thumb_path(char_dir, video.name)
    thumb.parent.mkdir(exist_ok=True)
    tmp = thumb.with_name(thumb.name + ".tmp.jpg")
    duration = _video_duration(video)
    positions = [min(90.0, duration * 0.3)] if duration else [60.0]
    positions.append(3.0)  # ripiego per video più corti del previsto
    for pos in positions:
        try:
            subprocess.run(
                ["nice", "-n", "19", "ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "1",
                 "-ss", f"{pos:.1f}", "-i", str(video), "-frames:v", "1", "-an", "-sn",
                 "-vf", f"scale={THUMB_WIDTH}:-2", "-q:v", "6", "-y", str(tmp)],
                capture_output=True, timeout=60,
            )
        except Exception as e:
            print(f"[thumb] ffmpeg fallito su {video.name}: {e}")
            continue
        if tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, thumb)
            return True
    tmp.unlink(missing_ok=True)
    return False

def _player_playing() -> bool:
    """True solo se un episodio sta andando adesso (in pausa la CPU è libera)."""
    try:
        with LAST_SEEN_TAG_FILE.open() as f:
            data = json.load(f)
    except Exception:
        return False
    return (time.time() - data.get("ts", 0)) <= 5 and data.get("mode") == "playing"

def _generate_thumbs_task(char_dir: Path, filenames: List[str]):
    """Background dopo un upload: anteprime dei nuovi episodi, se il lettore è libero."""
    if not _ffmpeg_available():
        return
    for name in filenames:
        video = char_dir / name
        if _player_playing() or not video.exists() or _thumb_version(char_dir, video):
            continue
        with _thumb_lock:
            _generate_thumb(char_dir, video)

def _thumb_worker():
    """
    Prepara piano piano le anteprime mancanti di tutti gli episodi, così la
    home ha quasi sempre l'immagine dell'episodio in onda. Una alla volta, con
    pause lunghe, e mai mentre un episodio è in riproduzione.
    """
    time.sleep(60)  # lascia finire l'avvio del dispositivo
    while True:
        try:
            if not _ffmpeg_available():
                time.sleep(3600)
                continue
            if _player_playing():
                time.sleep(30)
                continue
            missing = None
            if CHARACTERS_DIR.exists():
                for char_dir in sorted(CHARACTERS_DIR.iterdir()):
                    if not char_dir.is_dir():
                        continue
                    for video in sorted(_video_files(char_dir)):
                        if not _thumb_version(char_dir, video) and not _thumb_failed(char_dir, video):
                            missing = (char_dir, video)
                            break
                    if missing:
                        break
            if not missing:
                time.sleep(600)
                continue
            if _thumb_lock.acquire(blocking=False):
                try:
                    if not _generate_thumb(*missing):
                        _mark_thumb_failed(*missing)
                finally:
                    _thumb_lock.release()
            time.sleep(5)
        except Exception as e:
            print(f"[thumb] worker: {e}")
            time.sleep(60)

def _thumb_failed(char_dir: Path, video: Path) -> bool:
    """File che ffmpeg non sa leggere: il worker non ci riprova finché il video non cambia."""
    marker = _thumb_path(char_dir, video.name).with_suffix(".failed")
    try:
        return marker.stat().st_mtime >= video.stat().st_mtime
    except OSError:
        return False

def _mark_thumb_failed(char_dir: Path, video: Path):
    try:
        marker = _thumb_path(char_dir, video.name).with_suffix(".failed")
        marker.parent.mkdir(exist_ok=True)
        marker.touch()
    except OSError:
        pass

@app.on_event("startup")
def _start_thumb_worker():
    threading.Thread(target=_thumb_worker, name="thumb-worker", daemon=True).start()

def _video_files(char_dir: Path):
    return [p for p in char_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]

def load_time_limits():
    default = {"enabled": False, "days": {}, "exempt_characters": []}
    if TIME_LIMITS_FILE.exists():
        try:
            with TIME_LIMITS_FILE.open() as f:
                return json.load(f)
        except Exception as e:
            print(f"Errore nel leggere {TIME_LIMITS_FILE}: {e}")
            return default
    return default

def save_time_limits(data: dict):
    try:
        _write_json_atomic(TIME_LIMITS_FILE, data)
    except Exception as e:
        print(f"Errore nel salvare {TIME_LIMITS_FILE}: {e}")

_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')

def _validate_time_limits(cfg: TimeLimitsConfig):
    for day_key, day_cfg in cfg.days.items():
        if day_key not in DAY_KEYS:
            raise HTTPException(
                status_code=400,
                detail=f"Giorno non valido: '{day_key}' (attesi: {', '.join(DAY_KEYS)})"
            )
        if day_cfg.daily_limit_minutes is not None and day_cfg.daily_limit_minutes < 0:
            raise HTTPException(status_code=400, detail=f"Minuti giornalieri non validi per '{day_key}'.")

        for w in day_cfg.windows:
            if not _TIME_RE.match(w.start) or not _TIME_RE.match(w.end):
                raise HTTPException(status_code=400, detail=f"Orario non valido per '{day_key}' (formato HH:MM).")
            if w.end <= w.start:
                raise HTTPException(
                    status_code=400,
                    detail=f"L'orario di fine deve essere dopo l'inizio per '{day_key}'."
                )

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    """
    Serve il frontend (index.html) dalla cartella dell'API.
    """
    index_path = API_DIR / "index.html"
    if not index_path.exists():
        # fallback: messaggio semplice se manca il file
        return "<h1>cucu-device API</h1><p>index.html non trovato.</p>"
    return FileResponse(index_path)

@app.get("/api")
def api_root():
    return {"message": "cucu-device API attiva"}

# --- PIN genitore ------------------------------------------------------------
# Facoltativo: finché non viene impostato la UI resta aperta come prima.
# Non è sicurezza contro un attaccante (la pagina viaggia in HTTP sulla rete di
# casa): serve a evitare che bambini o ospiti cambino le impostazioni.
# Il PIN si recupera cancellando ui_auth.json via SSH (vedi CHEATSHEET.md).

SESSION_COOKIE = "cucu_session"
SESSION_DAYS = 90
PIN_ITERATIONS = 100_000  # PBKDF2: circa mezzo secondo sul Pi Zero 2 W, solo al login
_PIN_RE = re.compile(r"^\d{4,8}$")

# Percorsi sempre aperti: la pagina, i suoi asset e il login stesso
_PUBLIC_PATHS = {"/", "/api", "/manifest.webmanifest", "/auth/status", "/auth/login", "/auth/setup", "/auth/logout"}

_auth_cache = {"mtime": None, "data": None}

def load_ui_auth():
    """Contenuto di ui_auth.json (None se il PIN non è impostato), riletto solo se cambia."""
    try:
        mtime = UI_AUTH_FILE.stat().st_mtime_ns
    except OSError:
        _auth_cache.update(mtime=None, data=None)
        return None
    if mtime != _auth_cache["mtime"]:
        try:
            with UI_AUTH_FILE.open() as f:
                data = json.load(f)
        except Exception as e:
            print(f"Errore nel leggere {UI_AUTH_FILE}: {e}")
            data = None
        _auth_cache.update(mtime=mtime, data=data)
    return _auth_cache["data"]

def _hash_pin(pin: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations).hex()

def _pin_matches(auth: dict, pin: str) -> bool:
    candidate = _hash_pin(pin, bytes.fromhex(auth["salt"]), auth.get("iterations", PIN_ITERATIONS))
    return hmac.compare_digest(candidate, auth["hash"])

def _save_pin(pin: str):
    salt = secrets.token_bytes(16)
    _write_json_atomic(UI_AUTH_FILE, {
        "salt": salt.hex(),
        "hash": _hash_pin(pin, salt, PIN_ITERATIONS),
        "iterations": PIN_ITERATIONS,
        # Chiave per firmare le sessioni: nuova a ogni cambio PIN, così le
        # sessioni aperte con il PIN vecchio smettono di valere
        "secret": secrets.token_hex(32),
    })

def _session_token(auth: dict) -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    sig = hmac.new(bytes.fromhex(auth["secret"]), str(expires).encode(), "sha256").hexdigest()
    return f"{expires}.{sig}"

def _session_valid(auth: dict, token) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    if not expires.isdigit() or int(expires) < time.time():
        return False
    expected = hmac.new(bytes.fromhex(auth["secret"]), expires.encode(), "sha256").hexdigest()
    return hmac.compare_digest(sig, expected)

def _cookie_from_scope(scope) -> str:
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            for part in value.decode("latin-1").split(";"):
                name, _, val = part.strip().partition("=")
                if name == SESSION_COOKIE:
                    return val
    return ""

class PinGuard:
    """Middleware ASGI puro (non BaseHTTPMiddleware): lascia passare in streaming
    gli upload dei video senza bufferizzarli."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            auth = load_ui_auth()
            if auth and path not in _PUBLIC_PATHS and not path.startswith("/static/") \
                    and not _session_valid(auth, _cookie_from_scope(scope)):
                response = JSONResponse({"detail": "Serve il PIN"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

app.add_middleware(PinGuard)

# Tentativi sbagliati per indirizzo: dopo 5 errori si aspetta, sempre di più
_login_failures: Dict[str, dict] = {}

def _check_rate_limit(ip: str):
    entry = _login_failures.get(ip)
    if entry and entry["until"] > time.time():
        wait = int(entry["until"] - time.time()) + 1
        raise HTTPException(status_code=429, detail=f"Troppi tentativi. Riprova tra {wait} secondi.")

def _register_failure(ip: str):
    entry = _login_failures.setdefault(ip, {"count": 0, "until": 0})
    entry["count"] += 1
    if entry["count"] >= 5:
        entry["until"] = time.time() + min(900, 30 * 2 ** (entry["count"] - 5))

class PinPayload(BaseModel):
    pin: str

class PinChange(BaseModel):
    current_pin: str
    new_pin: str

def _validate_new_pin(pin: str):
    if not _PIN_RE.match(pin):
        raise HTTPException(status_code=400, detail="Il PIN deve avere da 4 a 8 cifre.")

def _with_session(payload: dict, auth: dict) -> JSONResponse:
    resp = JSONResponse(payload)
    resp.set_cookie(SESSION_COOKIE, _session_token(auth), max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="strict", path="/")
    return resp

@app.get("/auth/status")
def auth_status(request: Request):
    auth = load_ui_auth()
    return {
        "pin_set": auth is not None,
        "authenticated": auth is None or _session_valid(auth, request.cookies.get(SESSION_COOKIE)),
    }

@app.post("/auth/setup")
def auth_setup(payload: PinPayload):
    """Imposta il PIN la prima volta (dopo, solo /auth/change)."""
    if load_ui_auth() is not None:
        raise HTTPException(status_code=409, detail="Il PIN è già impostato.")
    _validate_new_pin(payload.pin)
    _save_pin(payload.pin)
    return _with_session({"status": "ok"}, load_ui_auth())

@app.post("/auth/login")
def auth_login(payload: PinPayload, request: Request):
    auth = load_ui_auth()
    if auth is None:
        return {"status": "ok"}
    ip = request.client.host if request.client else "?"
    _check_rate_limit(ip)
    if not _pin_matches(auth, payload.pin):
        _register_failure(ip)
        raise HTTPException(status_code=403, detail="PIN sbagliato.")
    _login_failures.pop(ip, None)
    return _with_session({"status": "ok"}, auth)

@app.post("/auth/logout")
def auth_logout():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

@app.post("/auth/change")
def auth_change(payload: PinChange, request: Request):
    auth = load_ui_auth()
    if auth is None:
        raise HTTPException(status_code=400, detail="Nessun PIN impostato.")
    ip = request.client.host if request.client else "?"
    _check_rate_limit(ip)
    if not _pin_matches(auth, payload.current_pin):
        _register_failure(ip)
        raise HTTPException(status_code=403, detail="Il PIN attuale non è giusto.")
    _validate_new_pin(payload.new_pin)
    _save_pin(payload.new_pin)
    return _with_session({"status": "ok"}, load_ui_auth())

@app.post("/auth/remove")
def auth_remove(payload: PinPayload, request: Request):
    auth = load_ui_auth()
    if auth is None:
        return {"status": "ok"}
    ip = request.client.host if request.client else "?"
    _check_rate_limit(ip)
    if not _pin_matches(auth, payload.pin):
        _register_failure(ip)
        raise HTTPException(status_code=403, detail="PIN sbagliato.")
    UI_AUTH_FILE.unlink(missing_ok=True)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp

# --- Installabile sulla schermata Home -----------------------------------------

@app.get("/manifest.webmanifest")
def web_manifest():
    return JSONResponse({
        "name": "Cucù",
        "short_name": "Cucù",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f5f6f3",
        "theme_color": "#f5f6f3",
        "lang": "it",
        "icons": [
            {"src": "/static/img/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/img/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/img/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, media_type="application/manifest+json")

@app.get("/system/info")
def system_info():
    """Dati del dispositivo per la UI: nome in rete, versione, canale OTA, spazio libero."""
    version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else ""
    cfg = dotenv_values(CONFIG_ENV_FILE) if CONFIG_ENV_FILE.exists() else {}
    try:
        disk = shutil.disk_usage(CHARACTERS_DIR if CHARACTERS_DIR.exists() else BASE_DIR)
        disk_info = {"free_bytes": disk.free, "total_bytes": disk.total}
    except OSError:
        disk_info = None
    return {
        "hostname": socket.gethostname(),
        "version": version or None,
        "channel": (cfg.get("UPDATE_CHANNEL") or "stable").strip(),
        "disk": disk_info,
        "thumbnails": _ffmpeg_available(),
    }


@app.get("/system/now")
def system_now():
    """
    Cosa succede adesso sulla TV, per la card "Ora sulla TV" della UI. Legge
    last_seen_tag.json (riscritto da read_nfc.py a 10 Hz): se è fermo da più di
    10 s il lettore non sta girando (stessa soglia di led.py).
    """
    data = {}
    try:
        with LAST_SEEN_TAG_FILE.open() as f:
            data = json.load(f)
    except Exception:
        pass  # file assente o letto a metà scrittura: si considera fermo

    alive = bool(data) and (time.time() - data.get("ts", 0)) <= 10
    mode = data.get("mode") if alive else None
    character = data.get("character") if mode in ("playing", "paused", "ended_wait_remove") else None
    episode = data.get("episode") if character else None

    usage = get_time_limits_usage()
    episode_state = load_episode_state()

    # Personaggio della statuina appoggiata adesso (anche se non sta partendo nulla)
    tag_character = None
    if alive and data.get("uid"):
        tags_map = load_tags()
        key = _find_uid(tags_map, data["uid"])
        tag_character = tags_map.get(key) if key else None

    # Anteprima dell'episodio solo se già pronta: mentre va il video non la si genera
    episode_thumb_v = None
    char_info = None
    if character:
        char_dir = CHARACTERS_DIR / character
        video = char_dir / episode if episode else None
        if video is not None and video.is_file():
            episode_thumb_v = _thumb_version(char_dir, video)
        if char_dir.is_dir():
            files = [p.name for p in _video_files(char_dir)]
            state = episode_state.get(character, {})
            remaining = set(state.get("remaining", []))
            known = set(state.get("known", []))
            char_info = {
                "total_episodes": len(files),
                "watched_in_round": sum(1 for f in files if f in known and f not in remaining),
            }

    # Prossima fascia oraria di oggi, per dire "si riparte alle 16:00"
    now_hm = datetime.now().strftime("%H:%M")
    starts = sorted(w["start"] for w in usage.get("windows", []) if w.get("start", "") > now_hm)

    return {
        "alive": alive,
        "mode": mode,
        "blocked": bool(data.get("blocked")) if alive else False,
        "tag_present": bool(data.get("uid")) if alive else False,
        "tag_character": tag_character,
        "character": character,
        "display_name": _display_name(episode_state, character) if character else None,
        "episode": episode,
        "episode_thumb_v": episode_thumb_v,
        "pos_ms": data.get("pos_ms") if character else None,
        "len_ms": data.get("len_ms") if character else None,
        "round": char_info,
        "next_window_start": starts[0] if starts else None,
        "usage": usage,
    }

@app.get("/characters")
def list_characters():
    episode_state = load_episode_state()
    tags_map = load_tags()

    characters = []

    if not CHARACTERS_DIR.exists():
        return characters

    for char_dir in CHARACTERS_DIR.iterdir():
        if not char_dir.is_dir():
            continue

        name = char_dir.name  # es. "peppa"
        state = episode_state.get(name, {})
        known = state.get("known", [])
        remaining = state.get("remaining", [])
        seen = state.get("seen", [])
        
        # Read display name from state, fallback to title case
        display_name = state.get("display_name", name.replace("_", " ").title())

        # conta quanti tag puntano a questo personaggio
        tags_count = sum(1 for uid, char in tags_map.items() if char == name)

        # Check image
        has_image = any((char_dir / f"profile{ext}").exists() for ext in IMAGE_EXT)
        image_url = f"/characters/{name}/image" if has_image else None

        characters.append({
            "name": name,
            "display_name": display_name,
            "active": True,  # per ora li consideriamo tutti attivi
            "has_image": has_image,
            "image_url": image_url,
            "stats": {
                "known": len(known),
                "remaining": len(remaining),
                "seen": len(seen),
            },
            "tags_count": tags_count,
        })

    # ordiniamo per nome giusto per estetica
    characters.sort(key=lambda c: c["name"])
    return characters

@app.post("/characters")
def create_character(payload: CharacterCreate):
    """
    Crea un nuovo personaggio:
    - crea la cartella characters/<name>
    - inizializza stato episodi vuoto in episode_state.json
    """
    raw_name = payload.name.strip()

    if not raw_name:
        raise HTTPException(status_code=400, detail="Il nome del personaggio non può essere vuoto.")

    # normalizziamo il nome: minuscolo, spazi -> underscore
    safe_name = raw_name.lower().replace(" ", "_")

    # controllino base sui caratteri ammessi
    allowed_chars = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if any(c not in allowed_chars for c in safe_name):
        raise HTTPException(
            status_code=400,
            detail="Il nome può contenere solo lettere minuscole, numeri, _ e -"
        )

    char_dir = CHARACTERS_DIR / safe_name

    # se la cartella esiste già, evitiamo di sovrascrivere
    if char_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Il personaggio '{safe_name}' esiste già."
        )

    # assicuriamoci che esista la cartella characters/
    CHARACTERS_DIR.mkdir(parents=True, exist_ok=True)

    # crea la cartella del personaggio
    try:
        char_dir.mkdir()
    except FileExistsError:
        # race condition improbabile, ma gestita
        pass
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nel creare la cartella del personaggio: {e}")

    # aggiorna episode_state
    episode_state = load_episode_state()

    if safe_name in episode_state:
        # esiste già nello stato ma non come cartella → situazione strana
        raise HTTPException(
            status_code=400,
            detail=f"Esiste già uno stato episodi per '{safe_name}', controlla i dati."
        )

    episode_state[safe_name] = {
        "known": [],
        "remaining": [],
        "seen": [],
        "display_name": payload.display_name or payload.name.strip()
    }
    save_episode_state(episode_state)

    # display_name di default che abbiamo salvato
    display_name = episode_state[safe_name]["display_name"]

    # per ora non gestiamo "active" da config, ma lo fissiamo a True
    return {
        "name": safe_name,
        "display_name": display_name,
        "active": True,
        "stats": {
            "known": 0,
            "remaining": 0,
            "seen": 0,
        },
        "tags_count": 0,
    }

class CharacterRename(BaseModel):
    new_name: str

@app.put("/characters/{name}")
def rename_character(name: str, payload: CharacterRename):
    """
    Rinomina un personaggio:
    - Rinomina la directory (se il nome safe cambia)
    - Aggiorna episode_state.json (sposta i dati e aggiorna display_name)
    - Aggiorna tags.json (se il nome safe cambia)
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    raw_new_name = payload.new_name.strip()
    if not raw_new_name:
        raise HTTPException(status_code=400, detail="Il nuovo nome non può essere vuoto.")
    
    # Normalizzazione per filesystem
    safe_new_name = raw_new_name.lower().replace(" ", "_")
    allowed_chars = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if any(c not in allowed_chars for c in safe_new_name):
        raise HTTPException(
            status_code=400,
            detail="Il nome (normalizzato) può contenere solo lettere minuscole, numeri, _ e -"
        )
    
    # Se il nome safe cambia, controlla collisioni
    rename_dir = (safe_new_name != name)
    new_char_dir = CHARACTERS_DIR / safe_new_name

    if rename_dir and new_char_dir.exists():
        raise HTTPException(status_code=400, detail=f"Esiste già un personaggio (directory) chiamato '{safe_new_name}'.")

    # 1. Rinomina directory (solo se cambia)
    if rename_dir:
        try:
            os.rename(char_dir, new_char_dir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Errore rinomina cartella: {e}")

    # 2. Aggiorna episode_state
    episode_state = load_episode_state()
    
    # Recupera i dati vecchi o creane di nuovi
    if name in episode_state:
        data = episode_state.pop(name)
    else:
        # Se non c'era stato, inizializzalo
        data = {"known": [], "remaining": [], "seen": []}
    
    # Aggiorna il display_name con quello fornito dall'utente (con maiuscole, spazi ecc)
    data["display_name"] = raw_new_name
    
    # Salva sotto il nuovo nome safe (o quello vecchio se non è cambiato)
    episode_state[safe_new_name] = data
    save_episode_state(episode_state)

    # 3. Aggiorna tags (solo se cambia nome safe)
    if rename_dir:
        tags_map = load_tags()
        updated_tags = False
        for uid, char in tags_map.items():
            if char == name:
                tags_map[uid] = safe_new_name
                updated_tags = True
        
        if updated_tags:
            save_tags(tags_map)

        # 4. Aggiorna l'eventuale esenzione dai limiti di tempo
        limits = load_time_limits()
        exempt_list = limits.get("exempt_characters", [])
        if name in exempt_list:
            exempt_list.remove(name)
            if safe_new_name not in exempt_list:
                exempt_list.append(safe_new_name)
            limits["exempt_characters"] = exempt_list
            save_time_limits(limits)

    return {
        "old_name": name,
        "new_name": safe_new_name,
        "display_name": raw_new_name,
        "status": "renamed"
    }

@app.get("/characters/{name}")
def get_character(name: str):
    """
    Ritorna la 'scheda' completa di un personaggio:
    - info base
    - tag NFC associati
    - lista episodi con stato (known/remaining/seen)
    - statistiche complessive
    """
    episode_state = load_episode_state()
    tags_map = load_tags()

    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    # Stato episodi per questo personaggio
    state = episode_state.get(name, {})
    known = state.get("known", [])
    remaining = state.get("remaining", [])
    seen = state.get("seen", [])

    # Episodi realmente presenti in cartella
    files = [
        p for p in char_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXT
    ]
    file_names = [p.name for p in files]

    # Tag NFC associati a questo personaggio, con l'eventuale nome dato dal genitore
    labels = load_tag_labels()
    tag_uids = [
        {"uid": uid, "label": labels.get(_norm_uid(uid), "")}
        for uid, char in tags_map.items()
        if char == name
    ]

    # Costruisci lista episodi con stato friendly. "watched": già visto nel giro
    # in corso (conosciuto e non più tra i rimanenti); i file non ancora nello
    # stato contano come da vedere, come fa read_nfc.py
    episodes = []
    for p in files:
        fname = p.name
        episodes.append({
            "filename": fname,
            "size_bytes": p.stat().st_size,
            "thumb_v": _thumb_version(char_dir, p),
            "watched": fname in known and fname not in remaining,
            "status": {
                "known": fname in known,
                "remaining": fname in remaining,
                "seen": fname in seen,
            }
        })

    # Statistiche (basate solo su file realmente presenti)
    total_episodes = len(file_names)
    watched_count = sum(1 for e in episodes if e["watched"])
    remaining_count = len([f for f in remaining if f in file_names])
    seen_count = len([f for f in seen if f in file_names])

    # Check image
    has_image = any((char_dir / f"profile{ext}").exists() for ext in IMAGE_EXT)
    image_url = f"/characters/{name}/image" if has_image else None
    
    display_name = state.get("display_name", name.replace("_", " ").title())

    time_limits = load_time_limits()

    return {
        "name": name,
        "display_name": display_name,
        "active": True,  # in futuro potremo leggere/scrivere da una config
        "has_image": has_image,
        "image_url": image_url,
        "time_limit_exempt": name in time_limits.get("exempt_characters", []),

        "tag_uids": tag_uids,

        "episodes": episodes,

        "stats": {
            "total_episodes": total_episodes,
            "remaining": remaining_count,
            "seen": seen_count,
            "watched_in_round": watched_count,
        }
    }

@app.put("/characters/{name}/time-limit-exempt")
def set_character_time_limit_exempt(name: str, payload: TimeLimitExempt, background_tasks: BackgroundTasks):
    """
    Esclude (o reinclude) questo personaggio dai limiti di tempo globali.
    Riavvia cucu-device.service per applicare subito, differendo il riavvio
    se il player sta riproducendo/in pausa un video in questo momento.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    limits = load_time_limits()
    exempt_list = limits.setdefault("exempt_characters", [])
    if payload.exempt and name not in exempt_list:
        exempt_list.append(name)
    elif not payload.exempt and name in exempt_list:
        exempt_list.remove(name)
    save_time_limits(limits)

    restart = _schedule_restart(background_tasks)

    return {"character": name, "time_limit_exempt": payload.exempt, "restart": restart}

@app.get("/characters/{name}/tags")
def get_character_tags(name: str):
    """Ritorna tutti gli UID associati a questo personaggio."""
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    tags_map = load_tags()
    tag_uids = [
        uid for uid, char in tags_map.items()
        if char == name
    ]

    return {
        "character": name,
        "tags": [{"uid": uid} for uid in tag_uids],
        "count": len(tag_uids),
    }

@app.post("/characters/{name}/tags")
def add_character_tag(name: str, payload: TagCreate):
    """
    Aggiunge un UID NFC a questo personaggio.
    Versione semplice: l'UID viene scritto a mano.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    # stesso formato dei lettori (esadecimale minuscolo), su cui si basa read_nfc.py
    if not payload.uid.strip():
        raise HTTPException(status_code=400, detail="UID non può essere vuoto.")
    uid_norm = _norm_uid(payload.uid)

    tags_map = load_tags()

    # se l'UID è già associato ad un altro personaggio, blocchiamo (salvo move)
    existing = _find_uid(tags_map, uid_norm)
    if existing is not None and tags_map[existing] != name and not payload.move:
        other = _display_name(load_episode_state(), tags_map[existing])
        raise HTTPException(
            status_code=400,
            detail=f"Questa statuina è già di {other}."
        )
    if existing is not None:
        del tags_map[existing]

    # associa questo UID al personaggio
    tags_map[uid_norm] = name
    save_tags(tags_map)

    # ritorniamo la lista aggiornata dei tag di questo personaggio
    tag_uids = [
        uid for uid, char in tags_map.items()
        if char == name
    ]

    return {
        "character": name,
        "tags": [{"uid": uid} for uid in tag_uids],
        "count": len(tag_uids),
    }

@app.delete("/characters/{name}/tags/{uid}")
def delete_character_tag(name: str, uid: str):
    """
    Rimuove l'associazione tra un UID e questo personaggio.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    tags_map = load_tags()

    # confronto senza maiuscole/spazi: i lettori scrivono l'UID in minuscolo
    uid_norm = _find_uid(tags_map, uid)
    if uid_norm is None:
        raise HTTPException(status_code=404, detail="UID non presente in tags.json.")

    if tags_map[uid_norm] != name:
        raise HTTPException(
            status_code=400,
            detail=f"Questo UID è associato a '{tags_map[uid_norm]}', non a '{name}'."
        )

    # rimuovi l'UID (e l'eventuale nome dato alla statuina)
    del tags_map[uid_norm]
    save_tags(tags_map)

    labels = load_tag_labels()
    if labels.pop(_norm_uid(uid_norm), None) is not None:
        save_tag_labels(labels)

    return {"character": name, "uid": uid_norm, "status": "removed"}

@app.put("/characters/{name}/tags/{uid}/label")
def set_character_tag_label(name: str, uid: str, payload: TagLabel):
    """
    Dà un nome a una statuina (es. "quella rossa", "di scorta"). Salvato a parte
    in tag_labels.json: tags.json resta nel formato UID → personaggio letto da read_nfc.py.
    """
    tags_map = load_tags()
    key = _find_uid(tags_map, uid)
    if key is None or tags_map[key] != name:
        raise HTTPException(status_code=404, detail="Statuina non trovata per questo personaggio.")

    label = " ".join(payload.label.split())[:40]
    labels = load_tag_labels()
    if label:
        labels[_norm_uid(key)] = label
    else:
        labels.pop(_norm_uid(key), None)
    save_tag_labels(labels)
    return {"character": name, "uid": key, "label": label}

@app.get("/system/scan-tag")
def scan_tag():
    """
    Legge l'ultimo tag visto dal lettore NFC (scritto da read_nfc.py).
    Usato dal wizard di associazione nella UI.
    Ritorna None se non c'è nessun tag o se il dato è troppo vecchio (>3s).
    """
    if not LAST_SEEN_TAG_FILE.exists():
        return {"uid": None, "known_character": None}

    try:
        with LAST_SEEN_TAG_FILE.open() as f:
            data = json.load(f)
    except Exception:
        return {"uid": None, "known_character": None}

    uid = data.get("uid")
    ts = data.get("ts", 0)

    if uid is None or (time.time() - ts) > 3:
        return {"uid": None, "known_character": None}

    tags_map = load_tags()
    key = _find_uid(tags_map, uid)
    known_character = tags_map[key] if key is not None else None

    return {
        "uid": uid,
        "known_character": known_character,
        "known_display_name": _display_name(load_episode_state(), known_character) if known_character else None,
    }

@app.get("/system/time-limits")
def get_time_limits():
    """Configurazione corrente dei limiti di tempo/fascia oraria."""
    limits = load_time_limits()
    return {
        "enabled": limits.get("enabled", False),
        "days": limits.get("days", {}),
        "exempt_characters": limits.get("exempt_characters", []),
    }

@app.post("/system/time-limits")
def set_time_limits(payload: TimeLimitsConfig, background_tasks: BackgroundTasks):
    """
    Salva la configurazione dei limiti di tempo e riavvia cucu-device.service
    per applicarla — subito se il player è libero, altrimenti al termine
    della visione in corso (non interrompiamo mai un episodio già avviato).
    """
    _validate_time_limits(payload)

    data = {
        "enabled": payload.enabled,
        "days": {k: v.dict() for k, v in payload.days.items()},
        "exempt_characters": payload.exempt_characters,
    }
    save_time_limits(data)

    restart = _schedule_restart(background_tasks)

    return {"status": "ok", "restart": restart}

@app.get("/system/time-limits/usage")
def get_time_limits_usage():
    """
    Stato di utilizzo per la giornata corrente (scritto da read_nfc.py), per
    mostrare al genitore quanti minuti restano oggi e la fascia oraria attiva.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    usage = {"date": today, "minutes": 0.0}
    if DAILY_USAGE_FILE.exists():
        try:
            with DAILY_USAGE_FILE.open() as f:
                loaded = json.load(f)
            if loaded.get("date") == today:
                usage = loaded
        except Exception:
            pass

    limits = load_time_limits()
    day_key = DAY_KEYS[datetime.now().weekday()]
    day_cfg = limits.get("days", {}).get(day_key, {}) if limits.get("enabled") else {}

    daily_limit = day_cfg.get("daily_limit_minutes")
    minutes_used = usage.get("minutes", 0.0)
    remaining = (daily_limit - minutes_used) if daily_limit is not None else None

    return {
        "date": today,
        "enabled": limits.get("enabled", False),
        "minutes_used": round(minutes_used, 1),
        "daily_limit_minutes": daily_limit,
        "remaining_minutes": round(remaining, 1) if remaining is not None else None,
        "windows": day_cfg.get("windows", []),
    }

@app.get("/characters/{name}/episodes")
def get_character_episodes(name: str):
    """
    Restituisce la lista degli episodi per un personaggio,
    con lo stato (known / remaining / seen) per ciascuno.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    episode_state = load_episode_state()
    state = episode_state.get(name, {"known": [], "remaining": [], "seen": []})
    known = state.get("known", [])
    remaining = state.get("remaining", [])
    seen = state.get("seen", [])

    files = [
        p for p in char_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXT
    ]

    episodes = []
    for p in files:
        fname = p.name
        episodes.append({
            "filename": fname,
            "status": {
                "known": fname in known,
                "remaining": fname in remaining,
                "seen": fname in seen,
            }
        })

    return {
        "character": name,
        "episodes": episodes,
        "stats": {
            "total": len(files),
            "known": len(known),
            "remaining": len(remaining),
            "seen": len(seen),
        }
    }


@app.post("/characters/{name}/episodes")
async def upload_character_episodes(
    name: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...)
):
    """
    Carica uno o più episodi per un personaggio.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    if not files:
        raise HTTPException(status_code=400, detail="Nessun file inviato.")

    episode_state = load_episode_state()
    state = episode_state.get(name, {"known": [], "remaining": [], "seen": []})
    known = state.get("known", [])
    remaining = state.get("remaining", [])
    seen = state.get("seen", [])

    saved_files = []

    for upload in files:
        original_name = os.path.basename(upload.filename)
        if not original_name:
            continue

        ext = os.path.splitext(original_name)[1].lower()
        if ext not in VIDEO_EXT:
            raise HTTPException(
                status_code=400,
                detail=f"Estensione non supportata per file '{original_name}'."
            )

        dest_path = char_dir / original_name
        if dest_path.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Esiste già un file chiamato '{original_name}' per '{name}'."
            )

        # Scrive su file temporaneo e rinomina solo a copia completata:
        # se il processo crasha a metà scrittura (es. OOM), non lascia un
        # file parziale che blocchi i tentativi successivi con "esiste già".
        tmp_path = dest_path.with_name(dest_path.name + ".part")
        try:
            with tmp_path.open("wb") as f:
                while chunk := await upload.read(1024 * 1024):
                    f.write(chunk)
            tmp_path.rename(dest_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        saved_files.append(original_name)

        if original_name not in known:
            known.append(original_name)
        if original_name not in remaining:
            remaining.append(original_name)

    new_state = {
        "known": known,
        "remaining": remaining,
        "seen": seen,
    }
    if "display_name" in state:
        new_state["display_name"] = state["display_name"]
    episode_state[name] = new_state
    save_episode_state(episode_state)

    background_tasks.add_task(_generate_thumbs_task, char_dir, saved_files)

    return {
        "character": name,
        "uploaded": saved_files,
        "stats": {
            "known": len(known),
            "remaining": len(remaining),
            "seen": len(seen),
        }
    }


@app.get("/characters/{name}/episodes/{filename}/thumb")
def get_episode_thumb(name: str, filename: str):
    """
    Anteprima dell'episodio. Se manca la crea (una alla volta): se il Cucù è
    occupato risponde 503 e la UI riprova dopo qualche secondo.
    """
    char_dir = CHARACTERS_DIR / name
    video = char_dir / os.path.basename(filename)
    if not video.is_file() or video.suffix.lower() not in VIDEO_EXT:
        raise HTTPException(status_code=404, detail="Episodio non trovato.")

    if _thumb_version(char_dir, video) is None:
        if _thumb_failed(char_dir, video):
            raise HTTPException(status_code=404, detail="Non riesco a estrarre un fotogramma da questo video.")
        if not _ffmpeg_available():
            raise HTTPException(status_code=404, detail="ffmpeg non installato: anteprime non disponibili.")
        if _player_playing():
            return JSONResponse({"detail": "Episodio in riproduzione: riprovo dopo."}, status_code=503, headers={"Retry-After": "20"})
        if not _thumb_lock.acquire(blocking=False):
            return JSONResponse({"detail": "Sto preparando un'altra anteprima."}, status_code=503, headers={"Retry-After": "3"})
        try:
            if _thumb_version(char_dir, video) is None and not _generate_thumb(char_dir, video):
                _mark_thumb_failed(char_dir, video)
                raise HTTPException(status_code=404, detail="Non riesco a estrarre un fotogramma da questo video.")
        finally:
            _thumb_lock.release()

    # L'URL cambia con la versione (?v=mtime), quindi il browser può tenerla a lungo
    return FileResponse(_thumb_path(char_dir, video.name), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

@app.post("/characters/{name}/episodes/reset-round")
def reset_character_round(name: str):
    """
    "Ricomincia il giro": tutti gli episodi tornano da vedere. Tocca solo la voce
    di questo personaggio in episode_state.json (mai reinizializzare il file);
    read_nfc.py rilegge lo stato dal disco prima di scegliere l'episodio, quindi
    vale dal prossimo episodio senza riavviare nulla.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    file_names = [p.name for p in _video_files(char_dir)]
    episode_state = load_episode_state()
    state = episode_state.get(name, {})
    known = [f for f in state.get("known", []) if f in file_names]
    known += [f for f in file_names if f not in known]

    episode_state[name] = {**state, "known": known, "remaining": known.copy(), "seen": state.get("seen", [])}
    save_episode_state(episode_state)

    return {"character": name, "status": "reset", "remaining": len(known)}

@app.patch("/characters/{name}/episodes/{filename}")
def rename_character_episode(name: str, filename: str, payload: EpisodeRename):
    """
    Rinomina un episodio (file) per un personaggio e aggiorna episode_state.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    old_name = os.path.basename(filename)
    new_name = os.path.basename(payload.new_filename.strip())

    if not new_name:
        raise HTTPException(status_code=400, detail="Il nuovo nome non può essere vuoto.")

    old_path = char_dir / old_name
    new_path = char_dir / new_name

    if not old_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{old_name}' non trovato per '{name}'.")

    if new_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Esiste già un file chiamato '{new_name}' per '{name}'."
        )

    ext = os.path.splitext(new_name)[1].lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"Estensione non supportata per file '{new_name}'."
        )

    try:
        os.rename(old_path, new_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nel rinominare il file: {e}")

    # l'anteprima segue il file
    old_thumb = _thumb_path(char_dir, old_name)
    if old_thumb.exists():
        try:
            os.replace(old_thumb, _thumb_path(char_dir, new_name))
        except OSError:
            pass

    episode_state = load_episode_state()
    state = episode_state.get(name, {"known": [], "remaining": [], "seen": []})
    known = [new_name if f == old_name else f for f in state.get("known", [])]
    remaining = [new_name if f == old_name else f for f in state.get("remaining", [])]
    seen = [new_name if f == old_name else f for f in state.get("seen", [])]

    new_state = {
        "known": known,
        "remaining": remaining,
        "seen": seen,
    }
    if "display_name" in state:
        new_state["display_name"] = state["display_name"]
    episode_state[name] = new_state
    save_episode_state(episode_state)

    return {
        "character": name,
        "old_filename": old_name,
        "new_filename": new_name,
    }


@app.delete("/characters/{name}/episodes/{filename}")
def delete_character_episode(name: str, filename: str):
    """
    Elimina un episodio (file) per un personaggio e aggiorna episode_state.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    file_name = os.path.basename(filename)
    file_path = char_dir / file_name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File '{file_name}' non trovato per '{name}'.")

    try:
        os.remove(file_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nell'eliminare il file: {e}")
    _thumb_path(char_dir, file_name).unlink(missing_ok=True)
    _thumb_path(char_dir, file_name).with_suffix(".failed").unlink(missing_ok=True)

    episode_state = load_episode_state()
    state = episode_state.get(name, {"known": [], "remaining": [], "seen": []})
    known = [f for f in state.get("known", []) if f != file_name]
    remaining = [f for f in state.get("remaining", []) if f != file_name]
    seen = [f for f in state.get("seen", []) if f != file_name]

    new_state = {
        "known": known,
        "remaining": remaining,
        "seen": seen,
    }
    if "display_name" in state:
        new_state["display_name"] = state["display_name"]
    episode_state[name] = new_state
    save_episode_state(episode_state)

    return {
        "character": name,
        "deleted": file_name,
        "stats": {
            "known": len(known),
            "remaining": len(remaining),
            "seen": len(seen),
        }
    }

@app.get("/characters/{name}/image")
def get_character_image(name: str):
    """
    Serve l'immagine di profilo del personaggio se esiste.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
         raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")
    
    for ext in IMAGE_EXT:
        img_path = char_dir / f"profile{ext}"
        if img_path.exists():
            return FileResponse(img_path)
            
    # Se non ha immagine, 404
    raise HTTPException(status_code=404, detail="Immagine non trovata")

@app.post("/characters/{name}/image")
async def upload_character_image(name: str, file: UploadFile = File(...)):
    """
    Carica l'immagine di profilo (profile.png/jpg/jpeg).
    Sovrascrive quella esistente.
    """
    char_dir = CHARACTERS_DIR / name
    if not char_dir.exists() or not char_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Personaggio '{name}' non trovato")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in IMAGE_EXT:
        raise HTTPException(status_code=400, detail="Formato non supportato (usa .png, .jpg, .jpeg)")
        
    # Rimuovi vecchie immagini per pulizia
    for e in IMAGE_EXT:
        old = char_dir / f"profile{e}"
        if old.exists():
            try: os.remove(old)
            except: pass

    dest = char_dir / f"profile{ext}"
    with dest.open("wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)
            
    return {"status": "ok", "filename": f"profile{ext}"}

def _restart_service_task():
    """Riavvia il servizio con un piccolo ritardo per permettere all'API di rispondere."""
    time.sleep(2)  # Delay per essere sicuri che la response 200 OK sia partita
    
    log_file = BASE_DIR / "restart.log"
    
    try:
        # Usiamo il path assoluto di systemctl se possibile, o lasciamo che il PATH lo trovi.
        # Spesso su Debian/Raspbian è /bin/systemctl o /usr/bin/systemctl.
        # Proviamo con un comando shell wrapper per catturare tutto.
        
        cmd = ["sudo", "systemctl", "restart", "cucu-device.service"]
        
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] Tentativo riavvio: {' '.join(cmd)}\n")
            
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
        
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] Successo.\n")
            
    except subprocess.CalledProcessError as e:
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] ERRORE exit code {e.returncode}:\nSTDERR: {e.stderr}\nSTDOUT: {e.stdout}\n")
    except Exception as e:
        with log_file.open("a") as f:
             f.write(f"[{time.ctime()}] EXCEPTION: {e}\n")

def _is_player_busy():
    """
    Legge il 'mode' scritto da read_nfc.py in last_seen_tag.json (ad ogni
    tick del loop, 10Hz) per capire se un video è in playing/paused. Un dato
    troppo vecchio (>5s) indica che il servizio non è verosimilmente in
    esecuzione: in quel caso non c'è nulla da interrompere.
    """
    if not LAST_SEEN_TAG_FILE.exists():
        return False
    try:
        with LAST_SEEN_TAG_FILE.open() as f:
            data = json.load(f)
    except Exception:
        return False
    ts = data.get("ts", 0)
    if (time.time() - ts) > 5:
        return False
    return data.get("mode") in ("playing", "paused")

def _restart_service_when_safe_task(max_wait_seconds=1800, poll_interval=5):
    """
    Come _restart_service_task, ma aspetta che il player non sia occupato
    (playing/paused) prima di riavviare, per non interrompere un episodio in
    corso. Se resta occupato oltre max_wait_seconds rinuncia: la config è già
    salvata su disco e verrà applicata al prossimo riavvio naturale del
    servizio (OTA, riavvio manuale, reboot), quindi non è mai persa.
    """
    log_file = BASE_DIR / "restart.log"
    waited = 0
    while _is_player_busy() and waited < max_wait_seconds:
        time.sleep(poll_interval)
        waited += poll_interval
    if _is_player_busy():
        try:
            with log_file.open("a") as f:
                f.write(f"[{time.ctime()}] Riavvio per limiti di tempo rinunciato dopo {waited}s: player ancora occupato.\n")
        except Exception:
            pass
        return
    _restart_service_task()

def _schedule_restart(background_tasks: BackgroundTasks) -> str:
    """Accoda il riavvio del player, subito o differito, e ritorna quale dei due."""
    if _is_player_busy():
        background_tasks.add_task(_restart_service_when_safe_task)
        return "deferred"
    background_tasks.add_task(_restart_service_task)
    return "immediate"

@app.delete("/characters/{name}")
def delete_character(name: str):
    """
    Elimina un personaggio:
    - Rimuove la cartella characters/<name>
    - Rimuove dal file episode_state.json
    - Rimuove i tag associati in tags.json
    """
    char_dir = CHARACTERS_DIR / name
    
    # Procediamo anche se la cartella non esiste, per pulire eventuali residui nei JSON
    if char_dir.exists():
        if not char_dir.is_dir():
             raise HTTPException(status_code=400, detail=f"'{name}' esiste ma non è una directory.")
        try:
            shutil.rmtree(char_dir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Errore eliminazione cartella: {e}")

    # 1. Pulizia episode_state
    episode_state = load_episode_state()
    if name in episode_state:
        del episode_state[name]
        save_episode_state(episode_state)
    
    # 2. Pulizia tags
    tags_map = load_tags()
    # Identifica chiavi da rimuovere
    uids_to_remove = [uid for uid, char in tags_map.items() if char == name]
    if uids_to_remove:
        for uid in uids_to_remove:
            del tags_map[uid]
        save_tags(tags_map)

    # 3. Pulizia nomi delle statuine tolte
    if uids_to_remove:
        labels = load_tag_labels()
        removed_labels = [labels.pop(_norm_uid(u), None) for u in uids_to_remove]
        if any(l is not None for l in removed_labels):
            save_tag_labels(labels)

    # 4. Pulizia esenzione limiti di tempo
    limits = load_time_limits()
    exempt_list = limits.get("exempt_characters", [])
    if name in exempt_list:
        exempt_list.remove(name)
        limits["exempt_characters"] = exempt_list
        save_time_limits(limits)

    return {"status": "deleted", "name": name, "tags_removed": len(uids_to_remove)}

@app.post("/system/restart-player")
def restart_player(background_tasks: BackgroundTasks):
    """
    Riavvia il servizio principale cucu-device (lettore NFC + riproduzione).
    Utile dopo modifiche a personaggi/episodi fatte via web.
    Usa un background task per non uccidere l'API prima della risposta.
    """
    print("DEBUG: Endpoint restart-player chiamato.")
    log_file = BASE_DIR / "restart.log"
    try:
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] Endpoint chiamato. Scheduling task...\n")
    except Exception as e:
        print(f"DEBUG: Impossibile scrivere log: {e}")

    background_tasks.add_task(_restart_service_task)
    return {"status": "ok", "message": "Riavvio in corso..."}


def _version_tuple(v: str):
    parts = v.strip().split(".")
    nums = [int(p) if p.isdigit() else 0 for p in parts[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


@app.get("/system/update-check")
def check_update():
    """
    Confronta VERSION locale con version.json del branch remoto, con lo stesso
    meccanismo usato da updater.sh, ma senza applicare nulla (sola lettura).
    """
    local_version = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else ""
    local_version = local_version or "0.0.0"

    cfg = dotenv_values(CONFIG_ENV_FILE) if CONFIG_ENV_FILE.exists() else {}
    repo_url = (cfg.get("REPO_URL") or "").strip()
    channel = (cfg.get("UPDATE_CHANNEL") or "stable").strip()

    if not repo_url:
        raise HTTPException(status_code=400, detail="REPO_URL non configurato in config.env")

    branch = "dev" if channel == "beta" else "main"
    repo_path = repo_url.rstrip("/")
    if "github.com/" in repo_path:
        repo_path = repo_path.split("github.com/", 1)[-1]
    if repo_path.endswith(".git"):
        repo_path = repo_path[:-4]

    version_json_url = f"https://raw.githubusercontent.com/{repo_path}/{branch}/version.json"

    try:
        req = urllib.request.Request(version_json_url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            remote_data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        raise HTTPException(status_code=502, detail=f"Impossibile controllare aggiornamenti: {e}")

    remote_version = str(remote_data.get("version") or "").strip()
    if not remote_version:
        raise HTTPException(status_code=502, detail="version.json remoto non valido")

    return {
        "current_version": local_version,
        "latest_version": remote_version,
        "update_available": _version_tuple(remote_version) > _version_tuple(local_version),
        "channel": channel,
        "changelog": remote_data.get("changelog"),
    }


def _update_service_task():
    """Avvia l'updater OTA con un piccolo ritardo per permettere all'API di rispondere."""
    time.sleep(2)
    log_file = BASE_DIR / "restart.log"
    cmd = ["sudo", "systemctl", "start", "cucu-device-updater.service"]
    try:
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] Avvio aggiornamento OTA: {' '.join(cmd)}\n")
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] cucu-device-updater.service avviato.\n")
    except subprocess.CalledProcessError as e:
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] ERRORE avvio updater: {e.stderr}\n")
    except Exception as e:
        with log_file.open("a") as f:
            f.write(f"[{time.ctime()}] EXCEPTION avvio updater: {e}\n")


@app.post("/system/update")
def start_update(background_tasks: BackgroundTasks):
    """
    Avvia subito un aggiornamento OTA lanciando cucu-device-updater.service,
    lo stesso oneshot usato dal timer notturno: gira come root e gestisce già
    fetch/reset, backup di tags.json, restart servizi e rollback automatico
    se l'health check fallisce.
    """
    background_tasks.add_task(_update_service_task)
    return {"status": "ok", "message": "Aggiornamento avviato..."}


# --- WIFI MANAGEMENT ---

class WifiConnect(BaseModel):
    ssid: str
    password: str

@app.get("/system/wifi")
def list_wifi():
    """
    Ritorna:
    - current: connessione attiva (SSID, segnale) o "Hotspot"
    - saved: lista connessioni salvate
    - scan: lista reti visibili al momento
    """
    # 1. Trova connessione attiva
    current = None
    try:
        # nmcli -t -f ACTIVE,SSID,SIGNAL,BARS dev wifi
        res = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,BARS", "dev", "wifi"],
            capture_output=True, text=True
        )
        for line in res.stdout.splitlines():
            # es: yes:Vodafone-123:80:▂▄▆_
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == "yes":
                current = {
                    "ssid": parts[1],
                    "signal": parts[2],
                    "bars": parts[3]
                }
                break
    except Exception as e:
        print(f"Errore check active wifi: {e}")

    # 2. Connessioni salvate
    saved = []
    try:
        # nmcli -t -f NAME,TYPE con show
        res = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "con", "show"],
            capture_output=True, text=True
        )
        for line in res.stdout.splitlines():
            # es: Vodafone-123:802-11-wireless
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "802-11-wireless":
                name = parts[0]
                # Filtra connessioni di sistema o hotspot che non vogliamo eliminare
                if name not in ("Hotspot", "CucuDevice_AP", "Cucu_AP", "preconfigured"):
                    saved.append(name)
    except Exception as e:
        print(f"Errore check saved wifi: {e}")

    # 3. Scansione reti (deduplica per SSID)
    scan_results = []
    seen_ssids = set()
    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,BARS,SECURITY", "dev", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True
        )
        for line in res.stdout.splitlines():
            # es: Vodafone-123:89:▂▄▆_:WPA2
            # Usa regex per splittare sui : non preceduti da \
            parts = re.split(r'(?<!\\):', line)
            
            # Pulisce eventuali escaped colons nei valori
            parts = [p.replace(r'\:', ':') for p in parts]

            if len(parts) >= 4:
                ssid = parts[0]
                if not ssid: continue # hidden network
                
                # Deduplica: mostra solo la più forte per ogni SSID
                if ssid in seen_ssids:
                    continue
                seen_ssids.add(ssid)
                
                scan_results.append({
                    "ssid": ssid,
                    "signal": parts[1],
                    "bars": parts[2],
                    "security": parts[3]
                })
    except Exception as e:
        print(f"Errore scan wifi: {e}")

    return {
        "current": current,
        "saved": saved,
        "scan": scan_results
    }


def _connect_wifi_task(ssid: str, password: str):
    """Logica di connessione eseguita in background."""
    print(f"[WiFi] Avvio connessione a '{ssid}'...")
    try:
        # 0. Abbatti l'hotspot se è attivo per liberare l'antenna wlan0
        ap_name = f"{socket.gethostname()}_AP"
        subprocess.run(["sudo", "nmcli", "con", "down", ap_name], capture_output=True)

        # 1. Elimina eventuale vecchia connessione con stesso nome
        subprocess.run(["sudo", "nmcli", "con", "delete", ssid], capture_output=True)
        
        # 2. Aggiungi nuova connessione
        subprocess.run(
            ["sudo", "nmcli", "con", "add", "type", "wifi", "ifname", "wlan0", 
             "con-name", ssid, "ssid", ssid],
            check=True, capture_output=True
        )
        
        # 3. Configura password (se presente)
        if password:
            subprocess.run(
                ["sudo", "nmcli", "con", "modify", ssid, "wifi-sec.key-mgmt", "wpa-psk"],
                check=True, capture_output=True
            )
            subprocess.run(
                ["sudo", "nmcli", "con", "modify", ssid, "wifi-sec.psk", password],
                check=True, capture_output=True
            )
            
        # 4. Imposta priorità alta
        subprocess.run(
            ["sudo", "nmcli", "con", "modify", ssid, "connection.autoconnect-priority", "100"],
             check=True, capture_output=True
        )
            
        # 5. Tenta connessione (questo fa cadere la rete attuale se su wlan0)
        # Usiamo un piccolo sleep prima per dare tempo all'API di rispondere 200 OK
        time.sleep(1) 
        
        subprocess.run(
            ["sudo", "nmcli", "con", "up", ssid],
            check=True, capture_output=True, timeout=30
        )
        print(f"[WiFi] Connessione a '{ssid}' completata con successo.")
        
    except Exception as e:
        print(f"[WiFi] Errore connessione a '{ssid}': {e}")


@app.post("/system/wifi")
def connect_wifi(payload: WifiConnect, background_tasks: BackgroundTasks):
    """
    Crea una nuova connessione WiFi e tenta di connettersi in BACKGROUND.
    Ritorna subito per evitare timeout del client quando cade la rete.
    """
    ssid = payload.ssid.strip()
    if not ssid:
        raise HTTPException(status_code=400, detail="SSID mancante")

    # Passiamo il compito al background
    background_tasks.add_task(_connect_wifi_task, ssid, payload.password)
    
    return {"status": "ok", "message": f"Tentativo di connessione a '{ssid}' avviato..."}

@app.post("/system/wifi/{ssid}/connect")
def switch_wifi(ssid: str, background_tasks: BackgroundTasks):
    """
    Si connette a una rete già salvata spegnendo l'AP.
    """
    if ssid.endswith("_AP"):
        raise HTTPException(status_code=400, detail="Non puoi connetterti manualmente all'Hotspot da qui.")
        
    def _switch_wifi_task(target_ssid: str):
        print(f"[WiFi] Switch a rete salvata '{target_ssid}'...")
        try:
            ap_name = f"{socket.gethostname()}_AP"
            subprocess.run(["sudo", "nmcli", "con", "down", ap_name], capture_output=True)
            time.sleep(1)
            subprocess.run(
                ["sudo", "nmcli", "con", "up", target_ssid],
                check=True, capture_output=True, timeout=30
            )
            print(f"[WiFi] Connesso a '{target_ssid}' completato.")
        except Exception as e:
            print(f"[WiFi] Errore switch a '{target_ssid}': {e}")

    background_tasks.add_task(_switch_wifi_task, ssid)
    return {"status": "ok", "message": f"Tentativo di passaggio a '{ssid}' avviato..."}


@app.delete("/system/wifi/{ssid}")
def forget_wifi(ssid: str):
    """
    Dimentica (elimina) una connessione salvata.
    """
    if ssid == "Hotspot" or ssid.endswith("_AP"):
        raise HTTPException(status_code=400, detail="Non puoi eliminare l'Hotspot di sistema da qui.")
        
    try:
        subprocess.run(
            ["sudo", "nmcli", "con", "delete", ssid],
            check=True, capture_output=True
        )
        return {"status": "ok", "deleted": ssid}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=404, detail=f"Errore (forse rete non trovata): {e}")
