from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional, List
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import time

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg"}

API_DIR = Path(__file__).resolve().parent

app = FastAPI()

# Percorsi base (stessi del tuo script NFC)
BASE_DIR = API_DIR.parent
CHARACTERS_DIR = BASE_DIR / "characters"
EPISODE_STATE_FILE = BASE_DIR / "episode_state.json"
TAGS_FILE = BASE_DIR / "tags.json"

class CharacterCreate(BaseModel):
    name: str
    display_name: Optional[str] = None

class TagCreate(BaseModel):
    uid: str

class EpisodeRename(BaseModel):
    new_filename: str

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
        with EPISODE_STATE_FILE.open("w") as f:
            json.dump(state, f)
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
        with TAGS_FILE.open("w") as f:
            json.dump(tags, f)
    except Exception as e:
        print(f"Errore nel salvare {TAGS_FILE}: {e}")

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

    # Tag NFC associati a questo personaggio
    tag_uids = [
        {"uid": uid}
        for uid, char in tags_map.items()
        if char == name
    ]

    # Costruisci lista episodi con stato friendly
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

    # Statistiche (basate solo su file realmente presenti)
    total_episodes = len(file_names)
    remaining_count = len([f for f in remaining if f in file_names])
    seen_count = len([f for f in seen if f in file_names])

    # Statistiche (basate solo su file realmente presenti)
    total_episodes = len(file_names)
    remaining_count = len([f for f in remaining if f in file_names])
    seen_count = len([f for f in seen if f in file_names])

    # Check image
    has_image = any((char_dir / f"profile{ext}").exists() for ext in IMAGE_EXT)
    image_url = f"/characters/{name}/image" if has_image else None
    
    display_name = state.get("display_name", name.replace("_", " ").title())

    return {
        "name": name,
        "display_name": display_name,
        "active": True,  # in futuro potremo leggere/scrivere da una config
        "has_image": has_image,
        "image_url": image_url,

        "tag_uids": tag_uids,

        "episodes": episodes,

        "stats": {
            "total_episodes": total_episodes,
            "remaining": remaining_count,
            "seen": seen_count,
        }
    }

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

    # normalizziamo un po' l'UID: togli spazi extra, metti tutto maiuscolo
    raw_uid = payload.uid.strip()
    if not raw_uid:
        raise HTTPException(status_code=400, detail="UID non può essere vuoto.")

    uid_norm = " ".join(raw_uid.split())

    tags_map = load_tags()

    # se l'UID è già associato ad un altro personaggio, blocchiamo
    if uid_norm in tags_map and tags_map[uid_norm] != name:
        raise HTTPException(
            status_code=400,
            detail=f"UID già associato al personaggio '{tags_map[uid_norm]}'."
        )

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

    # normalizza UID come nel POST
    uid_norm = " ".join(uid.strip().split()).upper()

    tags_map = load_tags()

    if uid_norm not in tags_map:
        raise HTTPException(status_code=404, detail="UID non presente in tags.json.")

    if tags_map[uid_norm] != name:
        raise HTTPException(
            status_code=400,
            detail=f"Questo UID è associato a '{tags_map[uid_norm]}', non a '{name}'."
        )

    # rimuovi l'UID
    del tags_map[uid_norm]
    save_tags(tags_map)

    return {"character": name, "uid": uid_norm, "status": "removed"}
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

        with dest_path.open("wb") as f:
            while chunk := await upload.read(1024 * 1024):
                f.write(chunk)

        saved_files.append(original_name)

        if original_name not in known:
            known.append(original_name)
        if original_name not in remaining:
            remaining.append(original_name)

    episode_state[name] = {
        "known": known,
        "remaining": remaining,
        "seen": seen,
    }
    save_episode_state(episode_state)

    return {
        "character": name,
        "uploaded": saved_files,
        "stats": {
            "known": len(known),
            "remaining": len(remaining),
            "seen": len(seen),
        }
    }


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

    episode_state = load_episode_state()
    state = episode_state.get(name, {"known": [], "remaining": [], "seen": []})
    known = [new_name if f == old_name else f for f in state.get("known", [])]
    remaining = [new_name if f == old_name else f for f in state.get("remaining", [])]
    seen = [new_name if f == old_name else f for f in state.get("seen", [])]

    episode_state[name] = {
        "known": known,
        "remaining": remaining,
        "seen": seen,
    }
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

    episode_state = load_episode_state()
    state = episode_state.get(name, {"known": [], "remaining": [], "seen": []})
    known = [f for f in state.get("known", []) if f != file_name]
    remaining = [f for f in state.get("remaining", []) if f != file_name]
    seen = [f for f in state.get("seen", []) if f != file_name]

    episode_state[name] = {
        "known": known,
        "remaining": remaining,
        "seen": seen,
    }
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

from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks

# ... (rest of imports)

def _connect_wifi_task(ssid: str, password: str):
    """Logica di connessione eseguita in background."""
    print(f"[WiFi] Avvio connessione a '{ssid}'...")
    try:
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

@app.delete("/system/wifi/{ssid}")
def forget_wifi(ssid: str):
    """
    Dimentica (elimina) una connessione salvata.
    """
    if ssid == "Hotspot":
        raise HTTPException(status_code=400, detail="Non puoi eliminare l'Hotspot di sistema da qui.")
        
    try:
        subprocess.run(
            ["sudo", "nmcli", "con", "delete", ssid],
            check=True, capture_output=True
        )
        return {"status": "ok", "deleted": ssid}
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=404, detail=f"Errore (forse rete non trovata): {e}")
