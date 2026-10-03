"""Cosa Cucù riesce a riprodurre: tipi di personaggio, foto degli album e
controllo dei video (Pi Zero 2 W).

Usato dall'API (al caricamento e dal worker delle anteprime) e da read_nfc.py
(per saltare i file che bloccherebbero la TV su un fotogramma). Le regole
vengono dal decoder hardware del Pi Zero 2 W, l'unico abbastanza veloce:
- solo H.264 (l'HEVC dell'iPhone non ha decoder hardware)
- al massimo 1080p, anche in verticale (un 4K resta fermo sul primo fotogramma)
- colori a 8 bit (l'HDR a 10 bit non è decodificabile in hardware)

L'esito si salva in characters/<nome>/.thumbs/<file>.check.json, valido
finché il video non cambia, così ffprobe gira una volta sola per file.
Solo stdlib: read_nfc.py gira col python di sistema, non nel venv dell'API.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

# --- Tipi di personaggio --------------------------------------------------
# Scelto alla creazione e salvato in episode_state.json ("kind") accanto a
# display_name. I personaggi creati prima non hanno il campo: sono cartoni.
#   video  → cartoni: un episodio alla volta, giro senza ripetizioni
#   photos → album: la statuina fa partire tutte le foto, PHOTO_SECONDS l'una
#   audio  → audio da ascoltare: un episodio alla volta come i cartoni, con
#            sulla TV la schermata "Si ascolta" (graphics/listen.png)
KINDS = ("video", "photos", "audio")
DEFAULT_KIND = "video"
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}
# .qta: i memo vocali delle versioni recenti di iOS (QuickTime audio); .caf e
# .aif/.aiff: altri formati Apple. ffprobe e VLC li leggono dal contenuto
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".flac", ".qta", ".caf", ".aif", ".aiff"}
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
PHOTO_SECONDS = 8
PROFILE_STEM = "profile"  # profile.jpg/png è l'immagine del personaggio, non una foto dell'album


def character_kind(state_entry):
    kind = (state_entry or {}).get("kind")
    return kind if kind in KINDS else DEFAULT_KIND


def episode_files(char_dir: Path, kind):
    """Episodi di un personaggio: video per i cartoni, audio per gli audio."""
    ext = AUDIO_EXT if kind == "audio" else VIDEO_EXT
    return [p for p in char_dir.iterdir() if p.is_file() and p.suffix.lower() in ext]


def photo_files(char_dir: Path):
    """Foto dell'album nell'ordine di caricamento (poi per nome): è l'ordine
    in cui il genitore le ha scelte, e l'API scrive i file uno alla volta."""
    files = [p for p in char_dir.iterdir()
             if p.is_file() and p.suffix.lower() in PHOTO_EXT and p.stem.lower() != PROFILE_STEM]
    return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))


MAX_LONG_SIDE = 1920
MAX_SHORT_SIDE = 1080
PLAYABLE_PIX_FMTS = {"yuv420p", "yuvj420p"}
CHECK_DIR_NAME = ".thumbs"  # la stessa cartella nascosta delle anteprime

CHECK_VERSION = 2  # esiti salvati con un formato più vecchio si ricalcolano

# Cosa fare, per ogni problema, sul telefono di chi carica: il motivo resta
# uno solo, il consiglio cambia (l'API riconosce il telefono dallo user
# agent). Stessi testi nella web UI per il controllo delle misure (howToFix)
FIX_1080P, FIX_H264, FIX_SDR = "1080p", "h264", "sdr"
HOW_TO = {
    "ios": {
        FIX_1080P: "Sull'iPhone: Impostazioni › Fotocamera › Registra video › 1080p a 30 fps.",
        FIX_H264: "Sull'iPhone: Impostazioni › Fotocamera › Formati › Più compatibile.",
        FIX_SDR: "Sull'iPhone: Impostazioni › Fotocamera › Registra video › disattiva Video HDR.",
    },
    "android": {
        FIX_1080P: "Sul telefono: nelle impostazioni della fotocamera scegli la risoluzione video 1080p (Full HD).",
        FIX_H264: "Sul telefono: nelle impostazioni della fotocamera disattiva i video ad alta efficienza (HEVC).",
        FIX_SDR: "Sul telefono: nelle impostazioni della fotocamera disattiva il video HDR (HDR10+ o 10 bit).",
    },
    None: {
        FIX_1080P: "Registra o esporta il video in 1080p.",
        FIX_H264: "Registra o esporta il video in H.264, il formato più compatibile.",
        FIX_SDR: "Registra o esporta il video senza HDR.",
    },
}


def platform_from_user_agent(user_agent):
    ua = user_agent or ""
    if "Android" in ua:
        return "android"
    if any(k in ua for k in ("iPhone", "iPad", "iPod")):
        return "ios"
    return None


def how_to(fix, platform=None):
    return HOW_TO.get(platform, HOW_TO[None]).get(fix, "") if fix else ""


def size_problem(width, height):
    """Motivo per cui misure così non si riproducono, oppure None."""
    if not width or not height:
        return None
    long_side, short_side = max(width, height), min(width, height)
    if long_side <= MAX_LONG_SIDE and short_side <= MAX_SHORT_SIDE:
        return None
    label = "4K" if long_side >= 3840 else "una risoluzione troppo alta"
    return f"è in {label} ({width}×{height}): Cucù riproduce video fino a 1080p."


def check_video(path, timeout=20):
    """(True, None, None) se si riproduce; (False, motivo, consiglio) se no, dove
    il consiglio è una chiave di HOW_TO (o None); (None, None, None) se non si
    può dire (ffprobe assente o bloccato): in quel caso non si scarta."""
    if shutil.which("ffprobe") is None:
        return None, None, None
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,pix_fmt", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        streams = json.loads(res.stdout or "{}").get("streams") or []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, None, None
    if not streams:
        return False, "non è un video che Cucù riesce a leggere.", None
    s = streams[0]
    codec = s.get("codec_name")
    if codec != "h264":
        name = "HEVC (H.265)" if codec == "hevc" else (codec or "un formato sconosciuto")
        return False, f"è in {name}: Cucù riproduce solo video H.264.", FIX_H264
    problem = size_problem(s.get("width"), s.get("height"))
    if problem:
        return False, problem, FIX_1080P
    if s.get("pix_fmt") not in PLAYABLE_PIX_FMTS:
        return False, "ha colori a 10 bit (HDR): Cucù riproduce solo video a 8 bit.", FIX_SDR
    return True, None, None


def check_audio(path, timeout=20):
    """Come check_video per gli audio: VLC li decodifica tutti in software, con
    poco lavoro, quindi basta che il file contenga davvero dell'audio."""
    if shutil.which("ffprobe") is None:
        return None, None, None
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
        streams = json.loads(res.stdout or "{}").get("streams") or []
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None, None, None
    if not streams:
        return False, "non è un audio che Cucù riesce a leggere.", None
    return True, None, None


def _check_path(video: Path) -> Path:
    return video.parent / CHECK_DIR_NAME / f"{video.name}.check.json"


def cached_check(video: Path):
    """Esito salvato se ancora valido (più recente del video e nel formato
    attuale): (ok, motivo, consiglio) o None."""
    cache = _check_path(video)
    try:
        if cache.stat().st_mtime < video.stat().st_mtime:
            return None
        with cache.open() as f:
            data = json.load(f)
        if data.get("v") != CHECK_VERSION:
            return None
        return data.get("ok"), data.get("reason"), data.get("fix")
    except (OSError, ValueError, AttributeError):
        return None


def save_check(video: Path, ok, reason, fix=None):
    """Scrittura atomica: API e read_nfc.py possono salvare lo stesso esito."""
    if ok is None:
        return  # non si sa: si riproverà
    cache = _check_path(video)
    try:
        cache.parent.mkdir(exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        with tmp.open("w") as f:
            json.dump({"v": CHECK_VERSION, "ok": ok, "reason": reason, "fix": fix}, f)
        os.replace(tmp, cache)
    except OSError:
        pass


def checked(video: Path):
    """Esito salvato, oppure controlla adesso e lo salva."""
    result = cached_check(video)
    if result is not None:
        return result
    ok, reason, fix = check_video(video)
    save_check(video, ok, reason, fix)
    return ok, reason, fix


def move_check(old: Path, new: Path):
    """Episodio rinominato: l'esito segue il file."""
    try:
        os.replace(_check_path(old), _check_path(new))
    except OSError:
        pass


def drop_check(video: Path):
    _check_path(video).unlink(missing_ok=True)
