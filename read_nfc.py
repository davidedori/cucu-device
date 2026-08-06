#!/usr/bin/env python3
import subprocess
import json
import re
import time
import random
import sys
from datetime import datetime
from pathlib import Path

# Tenta import vlc
try:
    import vlc
except ImportError:
    print("ERRORE CRITICO: Modulo 'vlc' non trovato.")
    print("Esegui: sudo apt install python3-vlc")
    sys.exit(1)

# --- CONFIG -------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "tags.json"
CHARACTERS_DIR = BASE_DIR / "characters"
GRAPHICS_DIR = BASE_DIR / "graphics"
TIME_LIMITS_PATH = BASE_DIR / "time_limits.json"
DAILY_USAGE_PATH = BASE_DIR / "daily_usage.json"

NFCLIST_PATH = "/usr/bin/nfc-list"
IDLE_IMAGE = GRAPHICS_DIR / "idle.png"
WAIT_NEXT_IMAGE = GRAPHICS_DIR / "wait_next.png"

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Persistiamo il conteggio minuti solo ogni tot secondi di riproduzione continua,
# per non stressare la SD card scrivendo ad ogni tick del loop (10Hz).
USAGE_FLUSH_INTERVAL_SEC = 10

# --- VLC PLAYER CLASS ---------------------------------------------------

class CucuPlayer:
    def __init__(self):
        # Parametri per full screen, no overlay, niente titolo
        # --mouse-hide-timeout=0 nasconde il mouse subito
        # --no-video-title-show nasconde il titolo del file
        # --image-duration=-1 fa durare le immagini per sempre (finché non stop)
        self.instance = vlc.Instance(
            "--fullscreen",
            "--no-video-title-show",
            "--quiet",
            "--mouse-hide-timeout=0",
            "--image-duration=-1" 
        )
        self.player = self.instance.media_player_new()
        # Event manager per intercettare fine video
        self.events = self.player.event_manager()
        self.events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end)
        
        self.has_ended = False

    def _on_end(self, event):
        self.has_ended = True

    def play_media(self, path: Path):
        """Riproduce un video o mostra un'immagine."""
        self.has_ended = False
        media = self.instance.media_new(str(path))
        self.player.set_media(media)
        self.player.play()
        # Imposta fullscreen ad ogni play per sicurezza
        self.player.set_fullscreen(True)

    def pause(self):
        self.player.set_pause(1)

    def resume(self):
        self.player.set_pause(0)

    def stop(self):
        self.player.stop()

    def is_playing(self):
        state = self.player.get_state()
        return state == vlc.State.Playing

    def check_ended(self):
        """Ritorna True se il media (video) è finito."""
        # Nota: per le immagini con duration -1 non finirà mai, 
        # ma per i video useremo i callback o lo stato
        if self.has_ended:
            self.has_ended = False # reset
            return True
        # Alternativa polling
        return self.player.get_state() == vlc.State.Ended

# --- STATO GLOBALE ------------------------------------------------------

player = CucuPlayer()
mode = "idle"
# mode values: "idle", "playing", "paused", "ended_wait_remove", "ended_wait_return"

current_character = None
current_video_path = None

last_uid = None
had_tag = False

# Stato episodi persistente
EPISODE_STATE_FILE = BASE_DIR / "episode_state.json"
episode_state = {} 

try:
    with CONFIG_PATH.open() as f:
        tag_map = json.load(f)
except FileNotFoundError:
    print(f"[WARN] {CONFIG_PATH} non trovato, parto con mappa vuota.")
    tag_map = {}
except json.JSONDecodeError as e:
    print(f"[ERROR] {CONFIG_PATH} corrotto ({e}), parto con mappa vuota.")
    tag_map = {}

# Limiti di tempo/fascia oraria (genitore). Fallback SEMPRE permissivo: un file
# assente o corrotto non deve mai bloccare l'avvio del servizio né la visione.
try:
    with TIME_LIMITS_PATH.open() as f:
        time_limits_config = json.load(f)
except FileNotFoundError:
    time_limits_config = {"enabled": False}
except json.JSONDecodeError as e:
    print(f"[ERROR] {TIME_LIMITS_PATH} corrotto ({e}), limiti disattivati.")
    time_limits_config = {"enabled": False}

# Minuti di riproduzione effettivi consumati nella giornata corrente.
try:
    with DAILY_USAGE_PATH.open() as f:
        daily_usage = json.load(f)
    if daily_usage.get("date") != datetime.now().strftime("%Y-%m-%d"):
        daily_usage = {"date": datetime.now().strftime("%Y-%m-%d"), "minutes": 0.0}
except (FileNotFoundError, json.JSONDecodeError):
    daily_usage = {"date": datetime.now().strftime("%Y-%m-%d"), "minutes": 0.0}

# --- FUNZIONI EPISODI ---------------------------------------------------

def save_episode_state():
    try:
        with EPISODE_STATE_FILE.open("w") as f:
            json.dump(episode_state, f)
    except Exception as e:
        print(f"Errore nel salvare {EPISODE_STATE_FILE}: {e}")

def save_daily_usage():
    try:
        with DAILY_USAGE_PATH.open("w") as f:
            json.dump(daily_usage, f)
    except Exception as e:
        print(f"Errore nel salvare {DAILY_USAGE_PATH}: {e}")

def load_episode_state():
    global episode_state
    if EPISODE_STATE_FILE.exists():
        try:
            with EPISODE_STATE_FILE.open() as f:
                episode_state = json.load(f)
        except Exception as e:
            print(f"Errore nel leggere {EPISODE_STATE_FILE}: {e}")
            episode_state = {}
    else:
        episode_state = {}

    # Sync
    for char_dir in CHARACTERS_DIR.iterdir():
        if not char_dir.is_dir(): continue
        character = char_dir.name
        files = [p.name for p in char_dir.iterdir() if p.suffix.lower() in VIDEO_EXT]
        if not files: continue

        state = episode_state.get(character, {})
        known = [f for f in state.get("known", []) if f in files]
        remaining = [f for f in state.get("remaining", []) if f in files]
        seen = [f for f in state.get("seen", []) if f in files]

        new_files = [f for f in files if f not in known]
        if new_files:
            known.extend(new_files)
            remaining.extend(new_files)

        def uniq(seq):
            out = []
            for x in seq:
                if x not in out: out.append(x)
            return out

        if character not in episode_state and known:
            remaining = known.copy()
            seen = []

        episode_state[character] = {
            "known": uniq(known),
            "remaining": uniq(remaining),
            "seen": uniq(seen)
        }
    save_episode_state()

def _select_episode(character: str):
    """Sceglie il prossimo episodio SENZA consumarlo dalla rotazione (nessuna
    scrittura su episode_state/disco). Ritorna (path, known, remaining, seen)
    da passare a _commit_episode() solo se si decide davvero di riprodurlo —
    così un episodio bloccato dai limiti di tempo non viene "sprecato"."""
    char_dir = CHARACTERS_DIR / character
    if not char_dir.exists(): return None
    files = [p for p in char_dir.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
    if not files: return None

    state = episode_state.get(character, {})
    known = state.get("known", [])
    remaining = state.get("remaining", [])
    seen = state.get("seen", [])

    if not known:
        known = [p.name for p in files]
        remaining = known.copy()
        seen = []
    else:
        file_names = [p.name for p in files]
        known = [f for f in known if f in file_names]
        remaining = [f for f in remaining if f in file_names]
        if not known: # reset totale se file cancellati
             known = file_names.copy()
             remaining = known.copy()
             seen = []

    if not remaining:
        remaining = known.copy()
        for f in known:
            if f not in seen: seen.append(f)
        print(f"Reset pool episodi per '{character}'")

    chosen_name = random.choice(remaining)
    remaining.remove(chosen_name)
    if chosen_name not in seen:
        seen.append(chosen_name)

    chosen_path = files[0]
    for p in files:
        if p.name == chosen_name:
            chosen_path = p
            break

    return chosen_path, known, remaining, seen

def _commit_episode(character: str, known, remaining, seen):
    """Persiste la scelta fatta da _select_episode(): va chiamata solo quando
    l'episodio sta davvero per partire."""
    episode_state[character] = {"known": known, "remaining": remaining, "seen": seen}
    save_episode_state()

def get_media_duration_minutes(path: Path):
    """Ritorna la durata del file in minuti, o None se non determinabile.
    Usa l'istanza VLC già in uso da CucuPlayer per non introdurre dipendenze
    aggiuntive (es. ffprobe) oltre a python3-vlc, già presente sul device."""
    try:
        media = player.instance.media_new(str(path))
        media.parse()
        length_ms = media.get_duration()
        if length_ms and length_ms > 0:
            return length_ms / 60000.0
    except Exception as e:
        print(f"Impossibile determinare la durata di {path.name}: {e}")
    return None

def _refresh_daily_usage_if_new_day():
    global daily_usage
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_usage.get("date") != today:
        daily_usage = {"date": today, "minutes": 0.0}
        save_daily_usage()

def is_viewing_allowed_now(character: str):
    """Ritorna (allowed, remaining_minutes). remaining_minutes è None quando
    non c'è un budget minuti da rispettare (nessun limite, o personaggio esente)."""
    if character in time_limits_config.get("exempt_characters", []):
        return True, None

    if not time_limits_config.get("enabled", False):
        return True, None

    day_cfg = time_limits_config.get("days", {}).get(DAY_KEYS[datetime.now().weekday()])
    if not day_cfg:
        return True, None

    now_time = datetime.now().time()
    window_start = day_cfg.get("window_start")
    window_end = day_cfg.get("window_end")
    if window_start and window_end:
        try:
            start_t = datetime.strptime(window_start, "%H:%M").time()
            end_t = datetime.strptime(window_end, "%H:%M").time()
            if not (start_t <= now_time <= end_t):
                return False, None
        except ValueError:
            pass  # config malformata: non blocchiamo per un orario illeggibile

    daily_limit = day_cfg.get("daily_limit_minutes")
    if daily_limit is not None:
        _refresh_daily_usage_if_new_day()
        remaining = daily_limit - daily_usage.get("minutes", 0.0)
        if remaining <= 0:
            return False, 0
        return True, remaining

    return True, None

def start_video(character):
    global current_character, mode, current_video_path
    allowed, remaining_minutes = is_viewing_allowed_now(character)
    if not allowed:
        print(f"Visione non permessa ora per '{character}' (limiti di tempo attivi).")
        return

    selection = _select_episode(character)
    if not selection:
        print(f"Nessun video trovato per {character}")
        return
    video, known, remaining, seen = selection

    if remaining_minutes is not None:
        duration_min = get_media_duration_minutes(video)
        if duration_min is not None and duration_min > remaining_minutes:
            print(
                f"Episodio '{video.name}' ({duration_min:.1f} min) supera il tempo "
                f"residuo oggi ({remaining_minutes:.1f} min): resto in idle."
            )
            return

    _commit_episode(character, known, remaining, seen)

    print(f"Riproduco video: {video.name}")
    player.play_media(video)
    current_character = character
    current_video_path = video
    mode = "playing"

def manage_graphics():
    """Gestisce la grafica in base allo stato, SE non stiamo riproducendo un video."""
    # Se stiamo riproducendo un video (playing/paused), lascia stare il player
    if mode in ("playing", "paused"):
        return

    # Logica per scegliere l'immagine (identica a prima)
    # idle -> idle.png
    # wait_remove / wait_return -> wait_next.png
    
    target_img = None
    if mode == "idle":
        # Se c'è un tag ma siamo in idle (caso strano o avvio), wait_next? 
        # Il vecchio codice diceva: se idle e has_tag -> wait_next, else idle
        if had_tag: 
            target_img = WAIT_NEXT_IMAGE
        else:
            target_img = IDLE_IMAGE
    elif mode in ("ended_wait_remove", "ended_wait_return"):
        target_img = WAIT_NEXT_IMAGE

    # Ottimizzazione: se l'immagine è già quella giusta, non ricaricarla.
    # Ma CucuPlayer non sa cosa sta suonando.
    # Possiamo tenere traccia qui o ricaricare sempre (python-vlc è veloce).
    # Per evitare flicker, ricarichiamo solo se necessario è meglio.
    # Tuttavia, player.set_media non causa flicker se è già quella.
    # Facciamo semplice: ricarichiamo se stato cambia.
    pass # In realtà con player unico, dobbiamo chiamare play_media quando entriamo nello stato

# Helper per refresh grafica
current_graphic_path = None
def refresh_graphic(force_path=None):
    global current_graphic_path
    path = force_path
    if not path:
        # Calcola path
        if mode == "idle" and not had_tag:
            path = IDLE_IMAGE
        else:
            path = WAIT_NEXT_IMAGE # Default per idle+tag o ended_*
    
    if path != current_graphic_path:
        print(f"Cambio grafica: {path.name}")
        player.play_media(path)
        current_graphic_path = path

# --- LETTORE NFC --------------------------------------------------------

def read_uid_once():
    try:
        result = subprocess.run([NFCLIST_PATH, "-v"], capture_output=True, text=True, timeout=2)
        out = result.stdout + result.stderr
        m = re.search(r"UID \(NFCID1\):\s*(.*)", out)
        if m:
            return " ".join(m.group(1).strip().split())
    except Exception:
        pass
    return None

# --- LOOP PRINCIPALE ----------------------------------------------------

print("cucu-device player avviato.")
load_episode_state()
refresh_graphic(IDLE_IMAGE) # Avvio con idle

last_tick_ts = time.time()
usage_unsaved_seconds = 0.0

try:
    while True:
        # 0. Accumulo minuti di riproduzione effettivi (esclusi personaggi
        # esenti e stato "paused"), basato sul tempo reale trascorso dal tick
        # precedente e non su un fisso 0.1s, per restare corretti anche se
        # un'iterazione impiega più del previsto (es. subprocess nfc-list lento).
        now_ts = time.time()
        elapsed = now_ts - last_tick_ts
        last_tick_ts = now_ts
        if mode == "playing" and current_character not in time_limits_config.get("exempt_characters", []):
            _refresh_daily_usage_if_new_day()
            daily_usage["minutes"] += elapsed / 60.0
            usage_unsaved_seconds += elapsed
            if usage_unsaved_seconds >= USAGE_FLUSH_INTERVAL_SEC:
                save_daily_usage()
                usage_unsaved_seconds = 0.0

        # 1. Controllo fine video
        if mode == "playing":
            if player.check_ended():
                print("Video finito.")
                mode = "ended_wait_remove"
                refresh_graphic(WAIT_NEXT_IMAGE) # Immediato switch
                save_daily_usage()
                usage_unsaved_seconds = 0.0

        # 2. Lettura NFC
        uid = read_uid_once()
        has_tag = uid is not None

        # Scrivi l'ultimo tag visto per il wizard di associazione della UI, e il
        # mode corrente: l'API lo legge per capire se il player è "occupato"
        # prima di riavviare il servizio (es. dopo un salvataggio dei limiti).
        try:
            with (BASE_DIR / "last_seen_tag.json").open("w") as _f:
                json.dump({"uid": uid, "ts": time.time(), "mode": mode}, _f)
        except Exception:
            pass

        # Detector cambio rapido (swap senza passare da None visibile)
        tag_swapped = has_tag and had_tag and (uid != last_uid)
        
        # tag_just_added: vero se nuovo tag O se swappato
        tag_just_added = (has_tag and not had_tag) or tag_swapped
        # tag_removed: vero se tolto O se swappato (conta come rimozione del vecchio)
        tag_removed = ((not has_tag) and had_tag) or tag_swapped
        
        # 3. Logica stati
        if mode == "idle":
            if tag_just_added:
                char = tag_map.get(uid)
                if char:
                    # START VIDEO
                    start_video(char)
                    current_graphic_path = None # reset grafica tracker
                else:
                    print(f"Tag sconosciuto: {uid}")
            elif tag_removed:
                refresh_graphic(IDLE_IMAGE) # Torna a idle puro se tolto tag sconosciuto
            elif has_tag and current_graphic_path != WAIT_NEXT_IMAGE:
                 # Se c'è tag (e non è partito video per qualche motivo), mostra wait
                 refresh_graphic(WAIT_NEXT_IMAGE)

        elif mode == "playing":
            if tag_removed:
                print("Pausa.")
                player.pause()
                mode = "paused"
                save_daily_usage()
                usage_unsaved_seconds = 0.0
            elif tag_just_added:
                # Se arriviamo qui senza tag_removed (improbabile con la logica sopra, ma possibile se had_tag=False)
                char = tag_map.get(uid)
                if char != current_character:
                    print(f"Ignorato cambio {char} durante play.")

        elif mode == "paused":
            # In pausa controlliamo lo STATO (has_tag) invece dell'evento, 
            # così gestiamo anche il caso "Fast Swap" dove l'evento added è stato consumato dal removed precedente
            if has_tag:
                char = tag_map.get(uid)
                if char == current_character:
                    print("Riprendo.")
                    player.resume()
                    mode = "playing"
                else:
                    # Se il tag è diverso, restiamo in pausa (Constraint Enforced)
                    # Non facciamo nulla, ignoriamo il nuovo tag
                    pass
            # Se tag rimosso, resta in pausa

        elif mode == "ended_wait_remove":
            # Aspetta che l'utente tolga il tag (o lo swappi)
            if tag_removed:
                print("Tag rimosso post-episodio.")
                mode = "ended_wait_return"
                # Grafica resta wait_next

        elif mode == "ended_wait_return":
            # Qui accettiamo qualsiasi NUOVO tag.
            # Usiamo has_tag per essere sicuri di prendere anche uno swap immediato
            if has_tag:
                char = tag_map.get(uid)
                if char:
                    print(f"Nuovo episodio per {char}")
                    start_video(char)
                    current_graphic_path = None
                else:
                    print("Tag sconosciuto post-episodio.")

        # Aggiorna tracking
        last_uid = uid
        had_tag = has_tag
        
        # Sleep rate veloce
        time.sleep(0.1)

except KeyboardInterrupt:
    print("Uscita...")
    player.stop()