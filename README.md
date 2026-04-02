# cucu-device

Dispositivo basato su Raspberry Pi che permette a un bambino di avviare video sul televisore appoggiando fisicamente una statuetta su un lettore NFC. Ogni statuetta è associata a un personaggio; appoggiandola il dispositivo fa partire l'episodio successivo di quel personaggio in modo sequenziale (senza tornare indietro).

---

## Requisiti hardware

| Componente | Note |
|---|---|
| Raspberry Pi 4 o 5 | Testato su RPi 4 Model B |
| Lettore NFC | Compatibile con `libnfc` (es. ACR122U) |
| Schermo/TV | Collegato via HDMI, fullscreen automatico |
| Scheda SD | 16 GB min, 32 GB consigliati (per i video) |
| Tag NFC | Uno per personaggio (NTAG215 o simili) |

---

## Struttura del repository

```
cucu-device/
├── read_nfc.py                 # Script principale: NFC reader + VLC player
├── tags.json                   # Mapping UID NFC → personaggio (configurato via UI)
├── VERSION                     # Versione corrente (es. 0.1.0)
├── version.json                # Manifest OTA: versione remota + changelog
├── requirements.txt            # Dipendenze Python per l'API
├── setup.sh                    # Script di installazione (idempotente)
├── updater.sh                  # Script aggiornamento OTA con fallback
├── setup_hotspot.sh            # Configurazione hotspot Wi-Fi di fallback
├── config.env.template         # Template configurazione OTA (copiare in config.env)
├── api/
│   ├── main.py                 # Server FastAPI (porta 8000)
│   └── index.html              # Frontend web (SPA single-file)
├── graphics/
│   ├── idle.png                # Schermata riposo (nessun tag)
│   ├── splash.png              # Splash screen al boot
│   └── wait_next.png           # Schermata "rimetti la statuetta"
├── characters/
│   └── <nome>/
│       ├── profile.png         # Immagine personaggio (usata dall'UI)
│       └── *.mp4               # Video episodi (non tracciati in git)
├── logs/                       # Log runtime (non tracciati in git)
└── systemd/
    ├── cucu-device.service     # Servizio NFC reader
    ├── cucu-device-api.service # Servizio API web
    ├── splashscreen.service    # Splash screen al boot
    ├── cucu-device-updater.service
    └── cucu-device-updater.timer
```

---

## Installazione su Raspberry Pi vergine

### 1. Requisiti iniziali

- Raspberry Pi OS Lite 64-bit installato e avviato
- Connessione SSH attiva o accesso diretto
- Utente non-root con sudo (es. `davidedorigatti`)

### 2. Clona il repository

```bash
cd ~
git clone https://github.com/davidedori/cucu-device
cd cucu-device
```

### 3. Esegui il setup

```bash
sudo bash setup.sh
```

Lo script è **idempotente**: può essere rieseguito senza danni in qualsiasi momento.

Cosa fa:
- Imposta l'hostname univoco `cucu-XXXX` (dagli ultimi 4 caratteri del MAC di wlan0)
- Installa le dipendenze di sistema (vlc, libnfc, fbi, avahi...)
- Crea la struttura cartelle in `/home/<utente>/cucu-device/`
- Crea il venv Python e installa le dipendenze API
- Configura i permessi sudoers per l'API
- Installa e abilita i servizi systemd
- Crea `config.env` da template (da completare con `REPO_URL`)

### 4. Aggiungi i video

```bash
mkdir -p ~/cucu-device/characters/peppa_pig
# copia i file .mp4 nella cartella del personaggio
```

### 5. Configura l'OTA

```bash
nano ~/cucu-device/config.env
# imposta REPO_URL=https://github.com/davidedori/cucu-device
```

### 6. Riavvia

```bash
sudo reboot
```

Il dispositivo sarà raggiungibile su `http://cucu-XXXX.local:8000`.

---

## Sviluppo locale

Il codice non ha dipendenze da hardware per l'API — `main.py` gira su qualsiasi macchina.
`read_nfc.py` richiede VLC e il lettore NFC fisico.

```bash
# Setup venv locale
cd api
python3 -m venv venv
source venv/bin/activate
pip install -r ../requirements.txt

# Avvia l'API in modalità sviluppo
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Per testare senza il Pi: imposta `BASE_DIR` in `main.py` a una cartella locale con la struttura attesa.

---

## Deploy su Raspberry Pi esistente

Se il Pi è già configurato e vuoi aggiornare manualmente il codice:

```bash
# Sul Pi
cd ~/cucu-device
git pull origin main
sudo bash setup.sh   # ridistribuisce i file e aggiorna i servizi

# oppure, solo per riavviare i servizi
sudo systemctl restart cucu-device.service
sudo systemctl restart cucu-device-api.service
```

---

## Gestione OTA

Gli aggiornamenti automatici vengono controllati ogni notte alle 3:00.

```bash
# Forza un aggiornamento manuale immediato
sudo bash ~/cucu-device/updater.sh

# Controlla lo stato del timer
systemctl status cucu-device-updater.timer

# Leggi il log aggiornamenti
tail -f ~/cucu-device/logs/updater.log
```

In caso di aggiornamento fallito, `updater.sh` esegue automaticamente il rollback al commit precedente.

### Canali

- `UPDATE_CHANNEL=stable` → segue `main` (default, consigliato)
- `UPDATE_CHANNEL=beta` → segue `dev` (ultime funzionalità, meno stabile)

---

## Hostname e rete

L'hostname è generato al primo `setup.sh` dalla formula:

```
cucu-<ultimi 4 hex del MAC wlan0>
```

Esempi: `cucu-f2b1`, `cucu-a3e9`. Raggiungibile via mDNS come `cucu-f2b1.local`.

Se non c'è rete Wi-Fi configurata, il Pi crea automaticamente un hotspot:

- SSID: `CucuDevice_AP`
- Password: `cucu-device`

Configura le reti Wi-Fi dall'interfaccia web (`/api/wifi`).

---

## Servizi systemd

| Servizio | Avvio | Descrizione |
|---|---|---|
| `cucu-device.service` | boot | NFC reader + VLC player |
| `cucu-device-api.service` | boot | FastAPI su porta 8000 |
| `splashscreen.service` | sysinit | Splash PNG su framebuffer |
| `cucu-device-updater.timer` | boot | Trigger OTA ogni notte alle 3:00 |

```bash
sudo systemctl status cucu-device.service
sudo journalctl -u cucu-device.service -f
```
