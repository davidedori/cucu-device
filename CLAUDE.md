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

- **Versione corrente:** vedi `VERSION` e `version.json` (aggiornata ad ogni rilascio, non tenerla allineata qui)
- **Branch attivi:**
  - `main` — codice stabile, canale OTA `stable`
  - `dev` — sviluppo attivo, canale OTA `beta`
- **Hardware di riferimento:** Raspberry Pi Zero 2 W (512MB RAM fisici, ~415MB disponibili dopo lo split GPU — dispositivo molto vincolato in RAM, non un Pi 4), lettore NFC PN532 su I2C (hardware v2; l'ACR122U USB del v1 resta supportato), TV via HDMI
- **OS:** Debian GNU/Linux 13 (trixie), kernel 6.12 aarch64
- **Python:** 3.13.5

Il refactor da "TinyWorlds" a "cucu-device" è stato completato. Tutti i path, nomi di servizi, variabili e stringhe UI sono stati aggiornati.

---

## Struttura del codice

### Componenti principali

**`read_nfc.py`** — il cuore del sistema. Gira come servizio systemd (`cucu-device.service`). Loop a 10 Hz che:
1. Legge il tag NFC corrente tramite il backend creato da `nfc_reader.create_reader()`: PN532 su I2C (driver nativo, solo stdlib) oppure ACR122U via `nfc-list -v`. Il backend si sceglie con `NFC_READER` in `config.env` (`auto` di default: usa il PN532 se risponde). Entrambi restituiscono l'UID nello stesso formato (`"04 a1 b2 ..."`), su cui si basa `tags.json`
2. Gestisce una macchina a stati con 5 stati: `idle`, `playing`, `paused`, `ended_wait_remove`, `ended_wait_return`
3. Controlla VLC tramite `python-vlc` (binding nativo, non subprocess)
4. Gestisce la sequenza degli episodi per ogni personaggio (round-robin senza ripetizioni, stato persistito in `episode_state.json`)

La classe principale si chiama `CucuPlayer`. Lo stato degli episodi viene caricato/salvato in `episode_state.json` (non tracciato in git, specifico del dispositivo). La web UI lo modifica mentre il servizio gira (upload, rinomina, "ricomincia il giro", nome del personaggio), quindi `read_nfc.py` lo **rilegge dal disco** prima di scegliere un episodio e lo riscrive toccando solo la voce del personaggio, conservando le altre chiavi (es. `display_name`). Anche `tags.json` viene ricaricato quando cambia (una `stat()` per tick): le statuine abbinate dal web funzionano senza riavviare il servizio. I JSON di stato si scrivono in modo atomico (file `.tmp` + `os.replace`), sia qui sia nell'API.

**`api/main.py`** — server FastAPI (porta 80), lanciato da `cucu-device-api.service` tramite uvicorn nel venv. Gestisce:
- CRUD personaggi (crea cartella in `characters/`, gestisce `tags.json`)
- Upload video (chunked, salva in `characters/<nome>/`)
- Associazione tag NFC a personaggi
- Riavvio del servizio NFC (`sudo systemctl restart cucu-device.service`)
- Gestione rete Wi-Fi via nmcli (scan, connessione, hotspot)
- Serve `index.html` come SPA alla root e gli asset in `api/static/` su `/static`
- `GET /system/info`: hostname, versione, canale OTA, spazio libero su disco
- Statuine: `move: true` nel POST sposta una statuina da un altro personaggio; `PUT /characters/{name}/tags/{uid}/label` le dà un nome. Gli UID si confrontano senza distinguere maiuscole/minuscole (i lettori scrivono esadecimale minuscolo)
- `POST /characters/{name}/episodes/reset-round`: tutti gli episodi tornano da vedere
- `GET /characters/{name}/episodes/{filename}/thumb`: anteprima JPEG 320px, creata con ffmpeg alla prima richiesta e salvata in `characters/<nome>/.thumbs/` (valida finché è più recente del video). Una alla volta (lock), `nice 19`, `-threads 1`, e 503 + `Retry-After` se un episodio è in riproduzione. Rinomina/eliminazione episodio spostano/cancellano l'anteprima
- `GET /system/now`: cosa succede sulla TV (legge `mode`, `character`, `episode`, `pos_ms`/`len_ms`, `blocked` da `last_seen_tag.json`) + statuina appoggiata, anteprima già pronta, giro e uso di oggi. Alimenta la pagina iniziale "Adesso" (TV disegnata come sul sito + render del dispositivo in `api/static/img/dispositivo.webp`)
- Un thread dell'API (`_thumb_worker`) prepara in background le anteprime mancanti, una ogni pochi secondi e mai durante la riproduzione; i file illeggibili vengono segnati con `.failed` e non ritentati
- PIN genitore facoltativo: `/auth/*` e il middleware ASGI `PinGuard` (puro ASGI, per non bufferizzare gli upload). Senza `ui_auth.json` non blocca nulla; con il PIN restano aperti solo `/`, `/static/*`, `/api`, `/manifest.webmanifest` e `/auth/*`. `updater.sh` e il suo health check non usano HTTP, quindi non ne sono toccati. PIN dimenticato: la stessa statuina appoggiata 5 volte in 15 s fa scrivere a `read_nfc.py` `pin_reset.json` (scadenza a 10 minuti, LED con doppio lampeggio); finché vale, `POST /auth/reset` accetta un PIN nuovo senza quello vecchio e poi cancella il file. Il gesto da solo non cambia nulla
- `GET /manifest.webmanifest`: icona sulla schermata Home (niente service worker: su `http://*.local` non sarebbe disponibile)

**`api/index.html`** — frontend SPA single-file (HTML/CSS/JS inline), mobile-first, con lo stile del sito (`cucu-website/DESIGN.md`): routing via hash (`#/personaggi`, `#/personaggi/<nome>`, `#/tempo`, `#/impostazioni`), DOM costruito con `h()` senza `innerHTML` sui dati utente. Nessuna dipendenza da npm o bundler. Si aggiorna via git pull come tutto il resto.

**`api/static/`** — font (Fraunces, Figtree) e logo serviti in locale da `/static`: la UI non deve dipendere da CDN, perché al primo setup il telefono è collegato all'hotspot del Cucù senza internet. `setup.sh` la copia insieme a `index.html`.

**`graphics/`** — schermate mostrate sulla TV da `read_nfc.py`: `idle.png` (appoggia una statuina), `end.png` (fine episodio, togli la statuina), `wait_next.png` (statuina tolta: un altro?), `rest.png` (statuina bloccata dai limiti di tempo), `splash.png`, e la clessidra `hourglass/hourglass_0..9.png` sovrapposta al video. Insieme ai fotogrammi di avvio `plymouth/boot_0N.png` si generano da `graphics/src/tv-screens.html` con `python3 graphics/src/render.py` (Chrome headless sul computer, non sul Pi), con lo stesso linguaggio del sito. Il gufetto dell'animazione di avvio sta in `graphics/src/boot-owl-0N.png`.

**`led.py`** — LED di stato (hardware v2), servizio `cucu-led.service` che parte presto nel boot (`DefaultDependencies=no`). Anima un LED su GPIO13 con il PWM hardware (sysfs `/sys/class/pwm`). Legge lo stato da `last_seen_tag.json` (`mode`, `blocked`, `ts`), che `read_nfc.py` riscrive ad ogni tick; se il file non viene aggiornato da 10 s → respiro veloce. Sui device senza PWM esce con 0. Non è nel health check OTA.

**`updater.sh`** — script OTA. Legge `config.env`, scarica `version.json` da GitHub raw, confronta versioni semver, fa `git fetch + git reset --hard`, ripristina `tags.json`, aggiorna pip e systemd, health check + rollback automatico in caso di fallimento.

**`setup.sh`** — script di installazione idempotente. Configura hostname, installa dipendenze, crea struttura, copia file, crea venv, configura sudoers, installa e abilita servizi systemd incluso il timer OTA.

### Path sul dispositivo

Tutto il progetto vive in `/home/davidedorigatti/cucu-device/`. Questo path è hardcoded nei file `.service` e negli script. Se si cambia l'utente o il path, vanno aggiornati:
- `systemd/cucu-device.service` (ExecStart, WorkingDirectory)
- `systemd/cucu-device-api.service` (WorkingDirectory, ExecStart venv)
- `systemd/splashscreen.service` (ExecStart path grafica)
- `systemd/cucu-device-updater.service` (ExecStart)
- `systemd/cucu-led.service` (ExecStart, WorkingDirectory)
- `read_nfc.py` riga `BASE_DIR`
- `api/main.py` riga `BASE_DIR`

### File di configurazione

| File | Tracciato in git | Descrizione |
|---|---|---|
| `tags.json` | Sì (default vuoto) | Mapping UID → personaggio, modificato dall'utente |
| `episode_state.json` | No | Stato episodi visti, generato a runtime |
| `tag_labels.json` | No | Nomi dati alle statuine dal genitore (UID → nome); `tags.json` resta UID → personaggio |
| `ui_auth.json` | No | PIN genitore della web UI (hash PBKDF2 + chiave delle sessioni). Se manca, la UI è aperta. Per azzerare un PIN dimenticato basta cancellarlo |
| `pin_reset.json` | No | Scadenza della finestra per un nuovo PIN, aperta dal gesto con la statuina (scritto da `read_nfc.py`, cancellato dall'API) |
| `config.env` | No | Configurazione OTA specifica del dispositivo |
| `config.env.template` | Sì | Template da cui generare `config.env` |
| `VERSION` | Sì | Versione corrente (plain text, es. `0.1.0`) |
| `version.json` | Sì | Manifest OTA con `version`, `min_version`, `changelog` |

Quando si rilascia una nuova versione, vanno aggiornati **entrambi** `VERSION` e `version.json`.

### Dipendenze Python

**Sistema (apt, non pip):**
- `python3-vlc` — binding VLC usato da `read_nfc.py`
- `libnfc-bin` — fornisce `/usr/bin/nfc-list` (backend ACR122U)
- `i2c-tools` — crea il gruppo `i2c` e fornisce `i2cdetect` (backend PN532)
- `fbi` — framebuffer image viewer per splash screen
- `ffmpeg` — anteprime degli episodi nella web UI (facoltativo: senza, la UI mostra un'icona). Non viene installato dall'OTA: sui dispositivi esistenti serve `sudo apt install ffmpeg` una volta

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
Le librerie Python per libnfc sono poco mantenute e richiedono build nativa. `nfc-list` è il tool ufficiale di libnfc, stabile e già presente come pacchetto Debian. Il polling ogni 100ms via subprocess è sufficiente per il caso d'uso. Vale solo per l'ACR122U (hardware v1).

**Perché un driver PN532 nativo invece di libnfc per l'hardware v2?**
Con `nfc-list` ogni poll riapre il bus e reinizializza il chip. Su I2C con il Zero 2 W sarebbe lento, e i driver `pn532_i2c` di libnfc sono noti per essere instabili. `nfc_reader.Pn532I2cReader` tiene la connessione aperta e usa solo la stdlib (`/dev/i2c-1` + ioctl), niente Blinka/CircuitPython che pesano in RAM. Gli errori I2C transitori (clock stretching del Pi) vengono assorbiti restituendo l'ultimo UID valido per qualche poll: altrimenti un glitch sembrerebbe una statuetta tolta e metterebbe in pausa il video.

**Perché `git reset --hard` invece di `git pull` nell'OTA?**
`git pull` può fallire in presenza di modifiche locali (es. `episode_state.json` se per errore finisce nello staging). `git reset --hard` + fetch è deterministico e garantisce che il codice sul dispositivo corrisponda esattamente a quello del branch remoto. I file dell'utente sono protetti dal meccanismo di backup/restore in `updater.sh`.

**Perché `index.html` è un file singolo invece di un'app React/Vue?**
Il frontend viene distribuito via git pull insieme al codice Python. Un file singolo non richiede build step, node_modules, bundler. Sul Pi non c'è npm e non ci deve essere.

**Perché la web UI è in HTTP e non HTTPS?**
Su `.local` non esiste un certificato valido. Senza HTTPS mancano service worker (niente pagina "Cucù non è raggiungibile" ad app chiusa: compare una pagina bianca) e `beforeinstallprompt` (niente pulsante di installazione su Android: la UI mostra una guida). L'unica soluzione vera (dominio pubblico per dispositivo + certificati, schema Plex) e perché non è stata fatta sono in `docs/https.md`.

**Perché il venv è in `api/venv/` e non nella root?**
`read_nfc.py` usa solo pacchetti di sistema (`python3-vlc`, via apt). Il venv serve solo per l'API FastAPI. Tenerli separati evita conflitti e rende più chiaro che `read_nfc.py` non dipende dal venv.
