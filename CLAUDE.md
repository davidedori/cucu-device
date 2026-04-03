# CLAUDE.md — cucu-device

Questo file è pensato per essere letto da un AI assistant all'inizio di ogni sessione di lavoro sul progetto. Contiene il contesto necessario per lavorare in modo coerente senza dover esplorare tutto il codice da zero.

---

## Contesto prodotto

**Cucù** è un dispositivo fisico pensato per bambini tra i 2 e i 6 anni. Funziona così: il bambino ha una collezione di statuette, ognuna associata a un personaggio (Peppa Pig, Bing, Bluey, ecc.). Appoggiando una statuetta sul lettore NFC, il televisore parte automaticamente con l'episodio successivo di quel personaggio.

Non ci sono schermi da toccare, menu da navigare, o interazioni digitali per il bambino: l'unica interfaccia è fisica, tattile, immediata. Il genitore gestisce la configurazione (associazione tag, aggiunta video, Wi-Fi) da una web UI accessibile in rete locale.

L'obiettivo del progetto è duplice:
1. Dare ai bambini un rapporto con i contenuti digitali che passa attraverso oggetti fisici, non attraverso schermi touchscreen.
2. Permettere ai genitori di controllare cosa guardano i figli senza sistemi di parental control complessi.

Il progetto è in fase prototipo/early product. Il codice è funzionante e viene usato quotidianamente su hardware reale.

---

## Stato attuale

- **Versione corrente:** `0.1.0` (vedi `VERSION` e `version.json`)
- **Branch attivi:**
  - `main` — codice stabile, canale OTA `stable`
  - `dev` — sviluppo attivo, canale OTA `beta`
- **Hardware di riferimento:** Raspberry Pi 4, lettore NFC ACR122U, TV via HDMI
- **OS:** Debian GNU/Linux 13 (trixie), kernel 6.12 aarch64
- **Python:** 3.13.5

Il refactor da "TinyWorlds" a "cucu-device" è stato completato. Tutti i path, nomi di servizi, variabili e stringhe UI sono stati aggiornati.

---

## Struttura del codice

### Componenti principali

**`read_nfc.py`** — il cuore del sistema. Gira come servizio systemd (`cucu-device.service`). Loop a 10 Hz che:
1. Legge il tag NFC corrente via `nfc-list -v` (subprocess, parsing regex dell'UID)
2. Gestisce una macchina a stati con 5 stati: `idle`, `playing`, `paused`, `ended_wait_remove`, `ended_wait_return`
3. Controlla VLC tramite `python-vlc` (binding nativo, non subprocess)
4. Gestisce la sequenza degli episodi per ogni personaggio (round-robin senza ripetizioni, stato persistito in `episode_state.json`)

La classe principale si chiama `CucuPlayer`. Lo stato degli episodi viene caricato/salvato in `episode_state.json` (non tracciato in git, specifico del dispositivo).

**`api/main.py`** — server FastAPI (porta 80), lanciato da `cucu-device-api.service` tramite uvicorn nel venv. Gestisce:
- CRUD personaggi (crea cartella in `characters/`, gestisce `tags.json`)
- Upload video (chunked, salva in `characters/<nome>/`)
- Associazione tag NFC a personaggi
- Riavvio del servizio NFC (`sudo systemctl restart cucu-device.service`)
- Gestione rete Wi-Fi via nmcli (scan, connessione, hotspot)
- Serve `index.html` come SPA alla root

**`api/index.html`** — frontend SPA single-file (912 righe, HTML/CSS/JS inline). Nessuna dipendenza da npm o bundler. Si aggiorna via git pull come tutto il resto.

**`updater.sh`** — script OTA. Legge `config.env`, scarica `version.json` da GitHub raw, confronta versioni semver, fa `git fetch + git reset --hard`, ripristina `tags.json`, aggiorna pip e systemd, health check + rollback automatico in caso di fallimento.

**`setup.sh`** — script di installazione idempotente. Configura hostname, installa dipendenze, crea struttura, copia file, crea venv, configura sudoers, installa e abilita servizi systemd incluso il timer OTA.

### Path sul dispositivo

Tutto il progetto vive in `/home/davidedorigatti/cucu-device/`. Questo path è hardcoded nei file `.service` e negli script. Se si cambia l'utente o il path, vanno aggiornati:
- `systemd/cucu-device.service` (ExecStart, WorkingDirectory)
- `systemd/cucu-device-api.service` (WorkingDirectory, ExecStart venv)
- `systemd/splashscreen.service` (ExecStart path grafica)
- `systemd/cucu-device-updater.service` (ExecStart)
- `read_nfc.py` riga `BASE_DIR`
- `api/main.py` riga `BASE_DIR`

### File di configurazione

| File | Tracciato in git | Descrizione |
|---|---|---|
| `tags.json` | Sì (default vuoto) | Mapping UID → personaggio, modificato dall'utente |
| `episode_state.json` | No | Stato episodi visti, generato a runtime |
| `config.env` | No | Configurazione OTA specifica del dispositivo |
| `config.env.template` | Sì | Template da cui generare `config.env` |
| `VERSION` | Sì | Versione corrente (plain text, es. `0.1.0`) |
| `version.json` | Sì | Manifest OTA con `version`, `min_version`, `changelog` |

Quando si rilascia una nuova versione, vanno aggiornati **entrambi** `VERSION` e `version.json`.

### Dipendenze Python

**Sistema (apt, non pip):**
- `python3-vlc` — binding VLC usato da `read_nfc.py`
- `libnfc-bin` — fornisce `/usr/bin/nfc-list`
- `fbi` — framebuffer image viewer per splash screen

**Venv API (`api/venv/`, non tracciato in git):**
- Vedi `requirements.txt` per la lista completa
- Principali: `fastapi`, `uvicorn[standard]`, `pydantic`, `python-dotenv`, `python-multipart`

---

## Convenzioni

- **Nome tecnico del progetto:** `cucu-device` (con trattino) per file, servizi systemd, path, nomi di repo.
- **Variabili e classi Python:** `cucu` senza trattino dove il trattino non è sintatticamente valido (es. `CucuPlayer`, `cucu_state`).
- **Nome del prodotto/UI:** `Cucù` (con accento) in testi visibili all'utente.
- **Branch:** `main` per stable, `dev` per sviluppo. Nessun altro branch persistente per ora.
- **Versioning:** semver (`MAJOR.MINOR.PATCH`). In `0.x` ogni rilascio può rompere la compatibilità con versioni precedenti.
- **Lingua del codice:** commenti in italiano, nomi variabili in inglese.
- **Commit:** messaggi in inglese, prefisso convenzionale (`feat:`, `fix:`, `refactor:`, `docs:`).

---

## Come testare

### API (senza hardware)

```bash
cd api && source venv/bin/activate
BASE_DIR=/tmp/cucu-test uvicorn main:app --reload
```

Crea la struttura attesa in `/tmp/cucu-test/` (cartelle `characters/`, file `tags.json` e `episode_state.json`).

### NFC reader (con hardware)

Il servizio gira sul Pi. Per debug:

```bash
sudo journalctl -u cucu-device.service -f
# oppure direttamente
sudo python3 /home/davidedorigatti/cucu-device/read_nfc.py
```

### OTA updater

```bash
# Test dry-run: controlla solo se c'è un aggiornamento senza applicarlo
AUTO_UPDATE=false bash ~/cucu-device/updater.sh

# Test completo (serve REPO_URL configurato in config.env)
sudo bash ~/cucu-device/updater.sh

# Controlla il log
tail -f ~/cucu-device/logs/updater.log
```

---

## Come fare deploy

### Nuova installazione

```bash
git clone https://github.com/davidedori/cucu-device ~/cucu-device
sudo bash ~/cucu-device/setup.sh
# configura config.env, aggiungi video, sudo reboot
```

### Aggiornamento manuale su Pi esistente

```bash
cd ~/cucu-device
git pull origin main
sudo bash setup.sh   # ridistribuisce file e servizi, idempotente
sudo systemctl restart cucu-device.service cucu-device-api.service
```

### Rilascio di una nuova versione

1. Sviluppa e testa su `dev`
2. Aggiorna `VERSION` (es. `0.2.0`)
3. Aggiorna `version.json` (stesso numero + changelog)
4. Merge `dev` → `main`
5. I dispositivi con `UPDATE_CHANNEL=stable` si aggiorneranno automaticamente entro la notte

---

## Cose da non rompere

Queste sono le invarianti critiche del sistema. Qualsiasi modifica al codice deve preservarle. Se una di queste smette di funzionare, il dispositivo si blocca e richiede intervento fisico.

### 1. Autostart al boot

`cucu-device.service` e `cucu-device-api.service` devono partire automaticamente al boot senza intervento umano. Il bambino accende la TV e il dispositivo è subito operativo.

- Non rimuovere `WantedBy=multi-user.target` dai file `.service`
- Non modificare `After=network.target` senza verificare le dipendenze di avvio
- `setup.sh` deve sempre chiamare `systemctl enable` su entrambi i servizi
- Se aggiungi dipendenze a `read_nfc.py` che richiedono rete o risorse hardware, aggiorna le dipendenze systemd di conseguenza

### 2. Fallback OTA

Se un aggiornamento rompe i servizi, `updater.sh` deve tornare al commit precedente funzionante. Il meccanismo di rollback in `updater.sh` è:

```
git reset --hard $PREV_COMMIT → restart servizi → verifica is-active
```

- Non semplificare o rimuovere il blocco `rollback()` in `updater.sh`
- `PREV_COMMIT` viene salvato prima di qualsiasi modifica al codice
- Il health check attende 10 secondi prima di dichiarare il fallimento: questo margine è necessario per i servizi lenti ad avviarsi. Non ridurlo sotto i 5 secondi.
- `tags.json` viene sempre ripristinato dal backup, sia in caso di successo che di rollback: è la configurazione del dispositivo, perderla richiederebbe di riassociare fisicamente tutti i tag

### 3. Idempotenza del setup

`setup.sh` deve poter essere rieseguito in qualsiasi momento senza rompere un'installazione funzionante. Questo è critico perché `updater.sh` potrebbe chiamarlo dopo un aggiornamento che include modifiche ai servizi.

Regole da rispettare:
- Usare sempre `copy_file()` invece di `cp` diretto per i file dove src potrebbe coincidere con dst
- Non cancellare `tags.json`, `episode_state.json`, `config.env` se già presenti
- `mkdir -p` con controllo esistenza prima di creare cartelle
- `systemctl enable` è già idempotente, ma `daemon-reload` va sempre chiamato dopo aver copiato i `.service`
- Il blocco sudoers viene riscritto ad ogni run: va bene, è deterministico

### 4. Unicità hostname

Ogni dispositivo deve avere un hostname distinto sulla rete locale per evitare conflitti mDNS quando più Cucù sono presenti nella stessa rete (scenario plausibile: famiglie con più figli, scuole, showroom).

- L'hostname è generato da `setup.sh` con la formula `cucu-<ultimi 4 hex MAC wlan0>`
- Non sostituire questo meccanismo con hostname statici o numerici sequenziali
- Il fallback (quando nessuna interfaccia è disponibile al momento del setup) genera un suffisso casuale: è accettabile, ma avvisa l'utente
- `avahi-daemon` deve restare tra le dipendenze apt: è quello che espone `<hostname>.local` sulla rete

### 5. Preservazione stato episodi

`episode_state.json` non va mai troncato o reinizializzato durante un aggiornamento. Contiene quale episodio ha già visto il bambino per ogni personaggio: perderlo significa ricominciare dall'inizio o rivedere episodi appena visti.

- `updater.sh` non tocca `episode_state.json` (non è in `VOLATILE_FILES`, non viene sovrascritto da `git reset --hard` perché è in `.gitignore`)
- Non aggiungere `episode_state.json` a `VOLATILE_FILES` in `updater.sh`
- Non aggiungere `episode_state.json` al repo git

---

## Decisioni architetturali rilevanti

**Perché `nfc-list` via subprocess invece di una libreria Python NFC?**
Le librerie Python per libnfc sono poco mantenute e richiedono build nativa. `nfc-list` è il tool ufficiale di libnfc, stabile e già presente come pacchetto Debian. Il polling ogni 100ms via subprocess è sufficiente per il caso d'uso.

**Perché `git reset --hard` invece di `git pull` nell'OTA?**
`git pull` può fallire in presenza di modifiche locali (es. `episode_state.json` se per errore finisce nello staging). `git reset --hard` + fetch è deterministico e garantisce che il codice sul dispositivo corrisponda esattamente a quello del branch remoto. I file dell'utente sono protetti dal meccanismo di backup/restore in `updater.sh`.

**Perché `index.html` è un file singolo invece di un'app React/Vue?**
Il frontend viene distribuito via git pull insieme al codice Python. Un file singolo non richiede build step, node_modules, bundler. Sul Pi non c'è npm e non ci deve essere.

**Perché il venv è in `api/venv/` e non nella root?**
`read_nfc.py` usa solo pacchetti di sistema (`python3-vlc`, via apt). Il venv serve solo per l'API FastAPI. Tenerli separati evita conflitti e rende più chiaro che `read_nfc.py` non dipende dal venv.
