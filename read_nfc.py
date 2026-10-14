#!/usr/bin/env python3
import json
import os
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

from nfc_reader import create_reader
from clock import clock_ok
from media_check import (cached_check, checked, character_kind, photo_files, PHOTO_SECONDS,
                         episode_files, VIDEO_EXT)

# --- CONFIG -------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "tags.json"
CHARACTERS_DIR = BASE_DIR / "characters"
GRAPHICS_DIR = BASE_DIR / "graphics"
TIME_LIMITS_PATH = BASE_DIR / "time_limits.json"
# Stato per l'API e il LED (last_seen_tag.json), riscritto a ogni tick (10 Hz):
# in RAM, in /run/cucu (creata da systemd con RuntimeDirectory=), per non
# consumare la scheda SD (sul disco erano ~2 GB al giorno anche a Cucù fermo).
# Senza /run/cucu (computer di prova, servizio non ancora aggiornato) resta
# accanto al codice. Lo cercano allo stesso modo api/main.py e led.py
RUN_DIR = Path("/run/cucu")
STATE_IN_RAM = RUN_DIR.is_dir() and os.access(RUN_DIR, os.W_OK)
STATE_PATH = (RUN_DIR if STATE_IN_RAM else BASE_DIR) / "last_seen_tag.json"
# Blocco durante la configurazione dall'app (per ora: abbinamento di una statuina).
# Lo scrive l'API con una scadenza di pochi secondi e lo rinnova finché il
# genitore sta configurando: se il telefono si blocca o perde la rete il blocco
# scade da solo. Accanto allo stato, quindi in RAM. Una scadenza oltre
# LOCK_MAX_SEC da ora (orologio spostato) non vale
LOCK_PATH = STATE_PATH.with_name("lock.json")
LOCK_MAX_SEC = 30
# Aggiornamento in corso: updater.sh (root) scrive qui il passo (1..UPDATE_STEPS)
# con una scadenza, e lo cancella quando finisce. Sempre in /run/cucu, che resta
# tra un riavvio e l'altro del servizio: appena riavviato dall'aggiornamento,
# read_nfc.py rimette subito la schermata. Stesse costanti in updater.sh
UPDATE_STATUS_PATH = RUN_DIR / "update_status.json"
UPDATE_STEPS = 4
UPDATE_STATUS_SEC = 600
# Schermate con testo e QR, che l'API disegna sul Pi (api/tv_screens.py) perché
# cambiano da dispositivo a dispositivo. Per ognuna: <nome>.json (c'è da mostrarla),
# <nome>_overlay.png (testo e QR, posati sul loop <nome>.mp4 con il filtro logo di
# VLC) e <nome>_screen.png (la schermata intera e ferma, se il loop manca).
# - done, "Fatto!": dopo un cambio di rete dall'app; scade dopo DONE_SEC, e l'API
#   lo toglie appena il telefono apre Cucù dal suo indirizzo .local
# - first, "Colleghiamoci!": finché Cucù non ha nessuna rete salvata (QR del Wi-Fi
#   dell'hotspot). Senza scadenza: lo ricalcola l'API
# Stesse costanti in api/main.py
DONE_SEC = 600
DONE_IMAGE = GRAPHICS_DIR / "done.png"     # sfondo senza testo, come il loop done.mp4
FIRST_IMAGE = GRAPHICS_DIR / "first.png"
TEXT_SCREENS = {DONE_IMAGE: "done", FIRST_IMAGE: "first"}
DAILY_USAGE_PATH = BASE_DIR / "daily_usage.json"

# Schermate sulla TV (sorgente e rigenerazione: graphics/src/). Si mostra il loop
# animato graphics/loops/<nome>.mp4 con lo stesso nome della PNG; la PNG resta
# come ripiego se il video manca
LOOPS_DIR = GRAPHICS_DIR / "loops"
IDLE_IMAGE = GRAPHICS_DIR / "idle.png"            # pronto: attesa, fine episodio, un altro?
REST_IMAGE = GRAPHICS_DIR / "rest.png"            # adesso no: il gufetto dorme (limiti di tempo)
LISTEN_IMAGE = GRAPHICS_DIR / "listen.png"        # audio in corso: "Si ascolta"
UNKNOWN_IMAGE = GRAPHICS_DIR / "unknown.png"      # statuina sconosciuta, finché resta appoggiata
SETUP_IMAGE = GRAPHICS_DIR / "setup.png"          # configurazione dall'app: Cucù non parte
# aggiornamento: update_1..UPDATE_STEPS.png, la barra si allunga a ogni passo di updater.sh
update_image = lambda step: GRAPHICS_DIR / f"update_{step}.png"

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
# Persistiamo il conteggio minuti solo ogni tot secondi di riproduzione continua,
# per non stressare la SD card scrivendo ad ogni tick del loop (10Hz). Si salva
# anche a ogni pausa e fine episodio: se manca la corrente si perde al massimo un minuto.
USAGE_FLUSH_INTERVAL_SEC = 60

# --- VLC PLAYER CLASS ---------------------------------------------------

def _logo_opt(name):
    """python-vlc ha rinominato i membri di VideoLogoOption tra le versioni
    ('enable' nella doc corrente, 'logo_enable' nella 3.0.21203 sul dispositivo)."""
    return getattr(vlc.VideoLogoOption, name, None) or getattr(vlc.VideoLogoOption, f"logo_{name}")

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
            "--image-duration=-1",
            # filtro logo: un'immagine sopra il video ("Fatto!", vedi want_overlay)
            "--sub-source=logo",
        )
        self.player = self.instance.media_player_new()
        # Event manager per intercettare fine video
        self.events = self.player.event_manager()
        self.events.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end)

        self.has_ended = False
        # Immagine da posare sul video con il filtro logo (want_overlay) e se è già posata
        self._overlay = None
        self._overlay_on = False
        # Secondo lettore, solo per l'audio: mostra il loop "si ascolta" mentre
        # self.player suona il file audio (vedi play_listen)
        self.screen = None

    def _on_end(self, event):
        self.has_ended = True

    def play_media(self, path: Path):
        """Riproduce un video o mostra un'immagine."""
        self._stop_screen()
        self._clear_overlay()
        self.has_ended = False
        media = self.instance.media_new(str(path))
        if path.suffix.lower() in VIDEO_EXT:
            # Demuxer di ffmpeg invece di quello mp4 di VLC: con i video senza
            # B-frame (quelli girati con l'iPhone) quello di VLC fa arrivare
            # sullo schermo metà dei fotogrammi, e il video va "al rallentatore"
            # con l'audio a tempo. Misurato sul Pi: 15 → 30 fps, invariati gli
            # episodi con B-frame, la durata, la pausa e l'evento di fine
            media.add_option(":demux=avformat")
        self.player.set_media(media)
        self.player.play()
        # Imposta fullscreen ad ogni play per sicurezza
        self.player.set_fullscreen(True)

    def _screen_media(self, image: Path):
        """Media di una schermata: il loop animato con lo stesso nome, ripetuto
        all'infinito, oppure la PNG se il loop non c'è."""
        loop = LOOPS_DIR / f"{image.stem}.mp4"
        if loop.exists():
            media = self.instance.media_new(str(loop))
            media.add_option(":input-repeat=65535")
            return media
        return self.instance.media_new(str(image))

    def want_overlay(self, path: Path):
        """Immagine sopra il video corrente (a tutto schermo, trasparente) con il filtro
        logo di VLC, come la vecchia clessidra. La posa apply_overlay(), dal ciclo
        principale, quando VLC ha già l'uscita video: impostata subito dopo play()
        non si vedeva."""
        self._overlay = path
        self._overlay_on = False

    def apply_overlay(self):
        if self._overlay is None or self._overlay_on or not self.player.has_vout():
            return
        self._overlay_on = True
        try:
            self.player.video_set_logo_string(_logo_opt("file"), str(self._overlay))
            self.player.video_set_logo_int(_logo_opt("position"), 5)  # in alto a sinistra
            self.player.video_set_logo_int(_logo_opt("x"), 0)
            self.player.video_set_logo_int(_logo_opt("y"), 0)
            self.player.video_set_logo_int(_logo_opt("opacity"), 255)
            self.player.video_set_logo_int(_logo_opt("enable"), 1)
            print(f"Overlay: {self._overlay.name}, enable={self.player.video_get_logo_int(_logo_opt('enable'))}")
        except Exception as e:
            print(f"[WARN] Overlay non disponibile: {e}")

    def _clear_overlay(self):
        was_on = self._overlay_on
        self._overlay = None
        self._overlay_on = False
        if was_on:
            try:
                self.player.video_set_logo_int(_logo_opt("enable"), 0)
            except Exception:
                pass

    def show_screen(self, image: Path):
        """Schermata a tutto schermo (attesa, fine, un altro?, riposo)."""
        self._stop_screen()
        self._clear_overlay()
        self.has_ended = False
        self.player.set_media(self._screen_media(image))
        self.player.play()
        self.player.set_fullscreen(True)

    def play_listen(self, path: Path):
        """Audio con la schermata "si ascolta" sulla TV (l'audio esce dall'HDMI,
        quindi qualcosa va mostrato). Due lettori: self.player suona l'audio
        (senza video, anche se il file ha una copertina) e da lui dipendono
        pausa, fine e posizione; self.screen mostra il loop animato. Con il
        loop agganciato all'audio (input-slave) la ripetizione del video
        ricomincerebbe anche l'audio"""
        self._stop_screen()
        self._clear_overlay()
        # Prima si libera lo schermo: self.player sta ancora mostrando la
        # schermata precedente e tiene l'uscita video (DRM). Se self.screen
        # parte mentre è occupata, VLC ripiega su un'uscita testuale (caca,
        # finisce nel journal) e la TV resta nera
        self.player.stop()
        self.has_ended = False
        image = LISTEN_IMAGE if LISTEN_IMAGE.exists() else IDLE_IMAGE
        self.screen = self.instance.media_player_new()
        self.screen.set_media(self._screen_media(image))
        self.screen.play()
        self.screen.set_fullscreen(True)
        media = self.instance.media_new(str(path))
        media.add_option(":no-video")
        self.player.set_media(media)
        self.player.play()

    def _stop_screen(self):
        if self.screen is None:
            return
        self.screen.stop()
        self.screen.release()
        self.screen = None

    def pause(self):
        self.player.set_pause(1)
        if self.screen is not None:
            self.screen.set_pause(1)

    def resume(self):
        self.player.set_pause(0)
        if self.screen is not None:
            self.screen.set_pause(0)

    def stop(self):
        self._stop_screen()
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
# Album di foto in corso: {"files": [Path], "index": foto mostrata, "shown": secondi
# già passati su questa foto}. None quando va un cartone o non va niente
album = None

last_uid = None
had_tag = False
# Statuina che non ha fatto partire niente (limiti di tempo, nessun episodio,
# sconosciuta): finché resta appoggiata non si ritenta a ogni tick
start_blocked_uid = None

# PIN genitore dimenticato: la stessa statuina appoggiata PIN_RESET_TAPS volte
# entro PIN_RESET_TAPS_SEC apre una finestra di PIN_RESET_SEC in cui la web UI
# accetta un PIN nuovo senza quello vecchio. Il gesto da solo non cambia nulla
# (se lo fa un bambino per gioco non succede niente): serve anche il telefono.
# La scadenza sta in pin_reset.json; l'API lo cancella quando il PIN è stato
# reimpostato. Stesse costanti in api/main.py.
UI_AUTH_FILE = BASE_DIR / "ui_auth.json"
PIN_RESET_FILE = BASE_DIR / "pin_reset.json"
PIN_RESET_TAPS = 5
PIN_RESET_TAPS_SEC = 15.0
PIN_RESET_SEC = 600
pin_reset_uid = None
pin_reset_taps = []  # istanti (monotoni) degli ultimi appoggi di pin_reset_uid

def track_pin_reset_gesture(uid):
    """Da chiamare a ogni statuina appoggiata (anche sconosciuta)."""
    global pin_reset_uid, pin_reset_taps
    now = time.monotonic()
    if uid != pin_reset_uid:
        pin_reset_uid, pin_reset_taps = uid, []
    pin_reset_taps = [t for t in pin_reset_taps if now - t < PIN_RESET_TAPS_SEC] + [now]
    if len(pin_reset_taps) < PIN_RESET_TAPS:
        return
    pin_reset_taps = []
    if not UI_AUTH_FILE.exists():
        return  # nessun PIN da azzerare
    try:
        _write_json_atomic(PIN_RESET_FILE, {"until": time.time() + PIN_RESET_SEC})
        print(f"Finestra per un nuovo PIN aperta per {PIN_RESET_SEC // 60} minuti.")
    except OSError as e:
        print(f"[WARN] Impossibile aprire la finestra per il nuovo PIN: {e}")

def pin_reset_open():
    """Vero se la finestra è aperta. Una scadenza oltre PIN_RESET_SEC da ora
    (orologio tornato indietro, il Pi non ha RTC) non vale."""
    try:
        with PIN_RESET_FILE.open() as f:
            until = float(json.load(f).get("until", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return 0 < until - time.time() <= PIN_RESET_SEC

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

# tags.json viene riletto quando cambia (statuine abbinate/tolte dalla web UI),
# così non serve riavviare il servizio. Basta una stat() per tick.
try:
    tags_mtime = CONFIG_PATH.stat().st_mtime_ns
except OSError:
    tags_mtime = None

def reload_tags_if_changed():
    global tag_map, tags_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        return
    if mtime == tags_mtime:
        return
    try:
        with CONFIG_PATH.open() as f:
            new_map = json.load(f)
    except (OSError, json.JSONDecodeError):
        return  # file illeggibile: tengo la mappa attuale e riprovo al prossimo tick
    tag_map = new_map
    tags_mtime = mtime
    print(f"tags.json ricaricato ({len(tag_map)} statuine).")

# Limiti di tempo/fascia oraria (genitore). Fallback SEMPRE permissivo: un file
# assente o corrotto non deve mai bloccare l'avvio del servizio né la visione.
# La web UI li cambia mentre il servizio gira: si ricaricano quando cambia il
# file (una stat() per tick, come tags.json), senza riavviare il servizio
time_limits_config = {"enabled": False}
time_limits_mtime = None

def reload_time_limits_if_changed():
    global time_limits_config, time_limits_mtime
    try:
        mtime = TIME_LIMITS_PATH.stat().st_mtime_ns
    except OSError:
        if time_limits_mtime is not None:
            time_limits_config, time_limits_mtime = {"enabled": False}, None
            print("time_limits.json rimosso: limiti disattivati.")
        return
    if mtime == time_limits_mtime:
        return
    time_limits_mtime = mtime
    try:
        with TIME_LIMITS_PATH.open() as f:
            time_limits_config = json.load(f)
        print("time_limits.json caricato.")
    except (OSError, json.JSONDecodeError) as e:
        print(f"[ERROR] {TIME_LIMITS_PATH} illeggibile ({e}), limiti disattivati.")
        time_limits_config = {"enabled": False}

reload_time_limits_if_changed()

# Minuti di riproduzione effettivi consumati nella giornata corrente.
try:
    with DAILY_USAGE_PATH.open() as f:
        daily_usage = json.load(f)
    if daily_usage.get("date") != datetime.now().strftime("%Y-%m-%d"):
        daily_usage = {"date": datetime.now().strftime("%Y-%m-%d"), "minutes": 0.0}
except (FileNotFoundError, json.JSONDecodeError):
    daily_usage = {"date": datetime.now().strftime("%Y-%m-%d"), "minutes": 0.0}

# --- FUNZIONI EPISODI ---------------------------------------------------

def _write_json_atomic(path: Path, data):
    """Scrive su un file temporaneo e lo rinomina: se manca la corrente a metà
    resta il file vecchio intero, mai uno troncato (vedi invariante 5)."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def write_state(data):
    """last_seen_tag.json: in RAM si scrive in modo atomico (chi legge non lo
    trova mai a metà); sulla SD, 10 volte al secondo, no: costerebbe un fsync
    a ogni tick, e chi legge già tiene lo stato precedente se lo trova vuoto."""
    if STATE_IN_RAM:
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, STATE_PATH)
    else:
        with STATE_PATH.open("w") as f:
            json.dump(data, f)

if STATE_IN_RAM:
    # quello vecchio sulla SD, fermo: l'API e il LED lo leggerebbero come se Cucù fosse spento
    try:
        (BASE_DIR / "last_seen_tag.json").unlink()
    except OSError:
        pass

def save_episode_state():
    try:
        _write_json_atomic(EPISODE_STATE_FILE, episode_state)
    except Exception as e:
        print(f"Errore nel salvare {EPISODE_STATE_FILE}: {e}")

def _read_episode_state_file():
    """Stato episodi letto dal disco: la web UI lo modifica mentre il servizio
    gira (upload, rinomina, "ricomincia il giro", nome del personaggio), quindi
    la copia in memoria non è la fonte di verità. Se il file è illeggibile si
    usa la copia in memoria, così non si perde nulla."""
    try:
        with EPISODE_STATE_FILE.open() as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"Errore nel rileggere {EPISODE_STATE_FILE}: {e}")
    return dict(episode_state)

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
        files = [p.name for p in episode_files(char_dir, character_kind(episode_state.get(character)))]
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

        # {**state, ...}: conserva le altre chiavi (es. display_name scritto dall'API)
        episode_state[character] = {
            **state,
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
    state = _read_episode_state_file().get(character, {})
    kind = character_kind(state)
    files = episode_files(char_dir, kind)
    # I video che Cucù non riesce a riprodurre (4K, HEVC, HDR: vedi
    # media_check.py) non entrano nel giro: bloccherebbero la TV su un fotogramma.
    # Gli audio non hanno questo controllo: VLC li legge tutti
    if kind == "video":
        files = [p for p in files if (cached_check(p) or (None,))[0] is not False]
    if not files: return None

    known = list(state.get("known", []))
    remaining = list(state.get("remaining", []))
    seen = list(state.get("seen", []))

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
        else:
            # File arrivati senza passare dall'API (es. copiati a mano):
            # entrano nel giro come episodi ancora da vedere
            new_files = [f for f in file_names if f not in known]
            known.extend(new_files)
            remaining.extend(new_files)

    if not remaining:
        remaining = known.copy()
        for f in known:
            if f not in seen: seen.append(f)
        print(f"Reset pool episodi per '{character}'")

    # Prima gli episodi mai visti (es. appena aggiunti dal genitore): "seen"
    # non si azzera con il giro, quindi distingue i nuovi da quelli già visti
    never_seen = [f for f in remaining if f not in seen]
    chosen_name = random.choice(never_seen or remaining)
    # Di solito l'esito c'è già (lo prepara l'API); se manca si controlla
    # adesso. Se il file non va, l'esito resta salvato e si sceglie di nuovo:
    # al giro dopo il file è escluso, quindi la ricorsione finisce
    if kind == "video" and checked(char_dir / chosen_name)[0] is False:
        print(f"Salto {chosen_name}: Cucù non riesce a riprodurlo")
        return _select_episode(character)
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
    global episode_state
    episode_state = _read_episode_state_file()
    entry = dict(episode_state.get(character, {}))  # conserva display_name
    entry.update(known=known, remaining=remaining, seen=seen)
    episode_state[character] = entry
    save_episode_state()

def _refresh_daily_usage_if_new_day():
    global daily_usage
    today = datetime.now().strftime("%Y-%m-%d")
    if daily_usage.get("date") != today:
        daily_usage = {"date": today, "minutes": 0.0}
        save_daily_usage()

def viewing_allowed(character):
    """Se adesso può partire un episodio di `character` (None: un personaggio
    qualsiasi non esente). Le regole:
    - fuori dalle fasce orarie di oggi, o finiti i minuti di oggi → no
    - un episodio già iniziato finisce sempre (qui non si guarda: la pausa
      riprende senza chiedere), e non c'è un tetto allo sforamento: basta un
      minuto rimasto per far partire un episodio lungo
    - ora non affidabile (clock.py) → nessun limite"""
    if character in time_limits_config.get("exempt_characters", []):
        return True
    if not time_limits_config.get("enabled", False):
        return True
    if not clock_ok():
        return True

    day_cfg = time_limits_config.get("days", {}).get(DAY_KEYS[datetime.now().weekday()])
    if not day_cfg:
        return True

    now_time = datetime.now().time()
    windows = day_cfg.get("windows", [])
    if windows:
        in_any_window = False
        for w in windows:
            try:
                start_t = datetime.strptime(w.get("start", ""), "%H:%M").time()
                end_t = datetime.strptime(w.get("end", ""), "%H:%M").time()
                if start_t <= now_time <= end_t:
                    in_any_window = True
                    break
            except ValueError:
                continue  # finestra malformata: ignorata, non blocca
        if not in_any_window:
            return False

    daily_limit = day_cfg.get("daily_limit_minutes")
    if daily_limit is not None:
        _refresh_daily_usage_if_new_day()
        try:
            if float(daily_usage.get("minutes", 0.0)) >= float(daily_limit):
                return False
        except (TypeError, ValueError):
            pass  # valore malformato: non blocca

    return True

def start_album(character):
    """Album di foto: tutte, nell'ordine di caricamento, PHOTO_SECONDS l'una.
    Ritorna "played" o "missing"."""
    global current_character, mode, current_video_path, album
    char_dir = CHARACTERS_DIR / character
    photos = photo_files(char_dir) if char_dir.is_dir() else []
    if not photos:
        print(f"Nessuna foto per {character}")
        return "missing"
    album = {"files": photos, "index": 0, "shown": 0.0}
    print(f"Album {character}: {len(photos)} foto")
    player.play_media(photos[0])
    current_character = character
    current_video_path = photos[0]
    mode = "playing"
    return "played"

def advance_album(elapsed):
    """Fa scorrere l'album di `elapsed` secondi di visione: dopo PHOTO_SECONDS
    passa alla foto dopo (saltando quelle eliminate nel frattempo). Ritorna
    True quando è finita l'ultima. Chiamata solo in "playing": in pausa la
    foto resta ferma e il suo tempo non avanza."""
    global current_video_path
    album["shown"] += elapsed
    if album["shown"] < PHOTO_SECONDS:
        return False
    album["shown"] = 0.0
    album["index"] += 1
    while album["index"] < len(album["files"]):
        photo = album["files"][album["index"]]
        if photo.exists():
            current_video_path = photo
            player.play_media(photo)
            return False
        album["index"] += 1
    return True

def start_video(character):
    """Ritorna "played", "blocked" (limiti di tempo) o "missing" (nessun video)."""
    global current_character, mode, current_video_path, album
    if not viewing_allowed(character):
        print(f"Visione non permessa ora per '{character}' (limiti di tempo attivi).")
        return "blocked"

    kind = character_kind(_read_episode_state_file().get(character))
    if kind == "photos":
        return start_album(character)
    album = None

    selection = _select_episode(character)
    if not selection:
        print(f"Nessun video trovato per {character}")
        return "missing"
    video, known, remaining, seen = selection
    _commit_episode(character, known, remaining, seen)

    if kind == "audio":
        print(f"Riproduco audio: {video.name}")
        player.play_listen(video)
    else:
        print(f"Riproduco video: {video.name}")
        player.play_media(video)
    current_character = character
    current_video_path = video
    mode = "playing"
    return "played"

# Schermata mostrata (per non ricaricarla a ogni tick)
current_graphic_path = None
def refresh_graphic(path):
    global current_graphic_path
    if path != current_graphic_path:
        print(f"Cambio grafica: {path.name}")
        name = TEXT_SCREENS.get(path)
        if name and not (LOOPS_DIR / f"{name}.mp4").exists():
            player.show_screen(RUN_DIR / f"{name}_screen.png")   # la schermata intera, ferma
        else:
            player.show_screen(path)
            if name:
                player.want_overlay(RUN_DIR / f"{name}_overlay.png")
        current_graphic_path = path

# Statuina sconosciuta appoggiata adesso (None: nessuna). La schermata resta
# finché non viene tolta: la TV dice sempre cosa c'è sopra Cucù
unknown_uid = None

def show_unknown(uid):
    """Statuina non abbinata, o personaggio senza episodi riproducibili: il
    bambino vede che Cucù l'ha letta, il genitore che c'è da sistemare."""
    global unknown_uid
    unknown_uid = uid

def lock_active():
    """Vero se l'app sta configurando Cucù (vedi LOCK_PATH)."""
    try:
        until = float(json.loads(LOCK_PATH.read_text()).get("until", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return 0 < until - time.time() <= LOCK_MAX_SEC

def update_step():
    """Passo dell'aggiornamento in corso (1..UPDATE_STEPS), o None (vedi UPDATE_STATUS_PATH)."""
    try:
        data = json.loads(UPDATE_STATUS_PATH.read_text())
        step, until = int(data.get("step", 0)), float(data.get("until", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    if not 0 < until - time.time() <= UPDATE_STATUS_SEC:
        return None
    return min(max(step, 1), UPDATE_STEPS)

def text_screen_active(name, max_sec=None):
    """Vero se l'API ha preparato la schermata con testo e QR `name` (vedi TEXT_SCREENS);
    con max_sec anche la scadenza ("until") deve valere."""
    try:
        data = json.loads((RUN_DIR / f"{name}.json").read_text())
        if max_sec is not None and not 0 < float(data.get("until", 0)) - time.time() <= max_sec:
            return False
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return (RUN_DIR / f"{name}_overlay.png").exists() and (RUN_DIR / f"{name}_screen.png").exists()

def idle_screen(blocked, locked=False, updating=None):
    """Schermata quando non va niente (né in riproduzione né in pausa): una
    sola, "pronto", per attesa, fine episodio e statuina tolta (al bambino
    serve solo riconoscere quando può appoggiarne una). Se adesso Cucù non si
    può usare il gufetto dorme, con o senza statuina: anche a fine episodio,
    che arriva sempre alla fine prima di questo. La statuina sconosciuta
    vince finché è appoggiata: dice qualcosa anche quando Cucù dorme.
    L'aggiornamento vince su tutto: il genitore deve vedere di non spegnere."""
    if updating:
        return update_image(updating)
    if locked:
        return SETUP_IMAGE
    if text_screen_active("done", DONE_SEC):
        return DONE_IMAGE
    if text_screen_active("first"):
        return FIRST_IMAGE
    if unknown_uid is not None:
        return UNKNOWN_IMAGE
    return REST_IMAGE if blocked else IDLE_IMAGE

# --- LOOP PRINCIPALE ----------------------------------------------------

print("cucu-device player avviato.")
load_episode_state()
# Avvio con "pronto", o con l'aggiornamento se è stato lui a riavviare il servizio
refresh_graphic(idle_screen(False, updating=update_step()))
# Dopo la grafica idle: il probe del PN532 può richiedere qualche istante (o
# riprovare a lungo se il chip non risponde) e lo schermo non deve restare
# grigio nel frattempo.
reader = create_reader()

last_tick_ts = time.monotonic()
usage_unsaved_seconds = 0.0

try:
    while True:
        # 0. Accumulo minuti di riproduzione effettivi (esclusi personaggi
        # esenti e stato "paused"), basato sul tempo reale trascorso dal tick
        # precedente e non su un fisso 0.1s, per restare corretti anche se
        # un'iterazione impiega più del previsto (es. lettura NFC lenta).
        # Orologio monotono: quando la rete corregge l'ora (il Pi non ha RTC)
        # time.time() salta anche di giorni, e il salto finiva tutto nei minuti
        # di visione (visto dal vivo: 3182 minuti in un giorno)
        now_ts = time.monotonic()
        elapsed = now_ts - last_tick_ts
        last_tick_ts = now_ts
        # Con l'ora non affidabile (clock.py) non si contano: finirebbero sul giorno sbagliato
        if mode == "playing" and current_character not in time_limits_config.get("exempt_characters", []) \
                and clock_ok():
            _refresh_daily_usage_if_new_day()
            daily_usage["minutes"] += elapsed / 60.0
            usage_unsaved_seconds += elapsed
            if usage_unsaved_seconds >= USAGE_FLUSH_INTERVAL_SEC:
                save_daily_usage()
                usage_unsaved_seconds = 0.0

        # 1. Controllo fine video (o fine album: le foto non "finiscono" da sole,
        # le fa scorrere advance_album; elapsed limitato contro i salti
        # dell'orologio, il Pi non ha RTC)
        if mode == "playing":
            if album is not None:
                ended = advance_album(min(max(elapsed, 0.0), 1.0))
                if ended:
                    print("Album finito.")
                    album = None
            else:
                ended = player.check_ended()
                if ended:
                    print("Video finito.")
            if ended:
                mode = "ended_wait_remove"  # la schermata la sceglie idle_screen()
                save_daily_usage()
                usage_unsaved_seconds = 0.0

        # 2. Lettura NFC
        reload_tags_if_changed()
        reload_time_limits_if_changed()
        uid = reader.read_uid()
        has_tag = uid is not None
        if unknown_uid is not None and uid != unknown_uid:
            unknown_uid = None  # tolta (o sostituita): via il punto di domanda

        # Scrivi l'ultimo tag visto per il wizard di associazione della UI, e il
        # mode corrente: l'API lo legge per capire se il player è "occupato"
        # prima di riavviare il servizio (es. dopo un salvataggio dei limiti).
        # "blocked" (visione bloccata ora dai limiti di tempo) e "ts" li usa
        # led.py per scegliere l'effetto del LED di stato.
        # "character"/"episode"/"pos_ms"/"len_ms" servono alla web UI per mostrare
        # cosa c'è sulla TV e a che punto è l'episodio. "pin_reset" (finestra per
        # un nuovo PIN aperta) fa lampeggiare il LED.
        try:
            blocked = not viewing_allowed(None)
        except Exception as e:
            print(f"[WARN] Limiti di tempo illeggibili ({e}): nessun limite.")
            blocked = False
        updating = update_step()
        if updating and mode in ("playing", "paused"):
            # il genitore ha scelto "Interrompi e aggiorna": fra poco l'aggiornamento
            # riavvia il servizio, intanto la TV dice di non spegnere. La statuina
            # rimasta appoggiata non fa ripartire niente (va tolta e rimessa)
            print("Aggiornamento in corso: episodio interrotto.")
            player.stop()
            mode = "idle"
            album = None
            current_character = current_video_path = None
            current_graphic_path = None
            start_blocked_uid = uid
            save_daily_usage()
            usage_unsaved_seconds = 0.0
        # durante l'aggiornamento, come durante la configurazione, non parte niente
        locked = lock_active() or updating is not None
        if locked:
            unknown_uid = None  # il genitore la sta abbinando: niente punto di domanda, nemmeno dopo
        try:
            pos_ms = len_ms = None
            if mode in ("playing", "paused") and album is not None:
                # avanzamento dell'album intero, non della singola foto
                pos_ms = int((album["index"] * PHOTO_SECONDS + album["shown"]) * 1000)
                len_ms = len(album["files"]) * PHOTO_SECONDS * 1000
            elif mode in ("playing", "paused"):
                pos_ms = player.player.get_time()
                len_ms = player.player.get_length()
            write_state({
                    "uid": uid, "ts": time.time(), "mode": mode, "blocked": blocked,
                    "clock_ok": clock_ok(),
                    "character": current_character,
                    "episode": current_video_path.name if current_video_path else None,
                    "pos_ms": pos_ms, "len_ms": len_ms,
                    "album": {"index": album["index"] + 1, "total": len(album["files"])} if album else None,
                    "pin_reset": pin_reset_open(),
                    "unknown": unknown_uid is not None,  # statuina sconosciuta: il LED lampeggia ogni tanto
                    "locked": locked,  # configurazione dall'app (o aggiornamento)
                    "updating": updating,  # passo dell'aggiornamento in corso, o None
                })
        except Exception:
            pass

        # Detector cambio rapido (swap senza passare da None visibile)
        tag_swapped = has_tag and had_tag and (uid != last_uid)
        
        # tag_just_added: vero se nuovo tag O se swappato
        tag_just_added = (has_tag and not had_tag) or tag_swapped
        # tag_removed: vero se tolto O se swappato (conta come rimozione del vecchio)
        tag_removed = ((not has_tag) and had_tag) or tag_swapped

        if tag_just_added:
            track_pin_reset_gesture(uid)

        # 3. Logica stati
        if mode == "idle":
            if tag_just_added and locked:
                # l'app sta configurando (es. abbina questa statuina): non parte
                # niente, e finita la configurazione va tolta e rimessa
                start_blocked_uid = uid
            elif tag_just_added:
                char = tag_map.get(uid)
                start_blocked_uid = None
                if char:
                    result = start_video(char)
                    if result == "played":
                        current_graphic_path = None  # la TV ora mostra l'episodio
                    elif result == "missing":
                        show_unknown(uid)
                else:
                    print(f"Tag sconosciuto: {uid}")
                    show_unknown(uid)
            elif tag_removed:
                start_blocked_uid = None

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

        elif mode == "ended_wait_return":
            # Qui accettiamo qualsiasi NUOVO tag.
            # Usiamo has_tag per essere sicuri di prendere anche uno swap immediato
            # (una statuina già rifiutata per i limiti non si ritenta a ogni tick)
            if has_tag and uid != start_blocked_uid and locked:
                start_blocked_uid = uid  # come in idle: durante il blocco non parte niente
            elif has_tag and uid != start_blocked_uid:
                char = tag_map.get(uid)
                if char:
                    print(f"Nuovo episodio per {char}")
                    result = start_video(char)
                    if result == "played":
                        current_graphic_path = None
                    else:
                        start_blocked_uid = uid
                        if result == "missing":
                            show_unknown(uid)
                else:
                    print("Tag sconosciuto post-episodio.")
                    start_blocked_uid = uid
                    show_unknown(uid)
            elif not has_tag and start_blocked_uid is not None:
                start_blocked_uid = None

        # Schermata quando non va niente: segue anche i cambi senza statuina
        # (si apre o si chiude una fascia oraria, finiscono i minuti)
        if mode not in ("playing", "paused"):
            refresh_graphic(idle_screen(blocked, locked, updating))
        player.apply_overlay()  # "Fatto!": testo e QR appena VLC ha l'uscita video

        # Aggiorna tracking
        last_uid = uid
        had_tag = has_tag
        
        # Sleep rate veloce
        time.sleep(0.1)

except KeyboardInterrupt:
    print("Uscita...")
    player.stop()