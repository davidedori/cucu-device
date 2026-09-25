# cucu-device

Dispositivo basato su Raspberry Pi che permette a un bambino di avviare video sul televisore appoggiando fisicamente una statuetta su un lettore NFC. Ogni statuetta è associata a un personaggio; appoggiandola il dispositivo fa partire l'episodio successivo di quel personaggio in modo sequenziale (senza tornare indietro).

---

## Requisiti hardware

| Componente | Note |
|---|---|
| Raspberry Pi 4 o 5 | Testato su RPi 4 Model B |
| Lettore NFC | PN532 su I2C (hardware v2) oppure ACR122U USB (hardware v1). Scelto da `NFC_READER` in `config.env`, default `auto` |
| Schermo/TV | Collegato via HDMI, fullscreen automatico |
| Scheda SD | 16 GB min, 32 GB consigliati (per i video) |
| Tag NFC | Uno per personaggio (NTAG215 o simili) |

### Collegamento PN532 (I2C)

Imposta il DIP switch del modulo in modalità **I2C**: tabellina serigrafata `I2C = 1 0`, cioè levetta 1 su ON e levetta 2 su OFF. Il chip legge il DIP switch solo all'accensione, quindi dopo averlo cambiato togli e ridai corrente.

Sul modulo si usa solo la fila da 4 pin (`GND VCC SDA SCL`). La fila da 8 serve per SPI, IRQ e reset e non va collegata.

Sul Pi Zero 2 W tutti i collegamenti stanno sulla **fila interna** del connettore GPIO (pin dispari, lato opposto al bordo della scheda). Il pin 1 ha la piazzola quadrata, dal lato della SD. Basta quindi un'unica fila di piedini, anche a 90°, saldata nei fori 1-3-5-7-9-11 (i pin 7 e 11 restano liberi), più i due piedini singoli 25 e 33 per il LED di stato.

| PN532 | Raspberry Pi (header GPIO) |
|---|---|
| VCC | 3V3, pin 1 (**non 5V**: i pull-up I2C del modulo vanno a VCC) |
| SDA | GPIO2 / SDA, pin 3 |
| SCL | GPIO3 / SCL, pin 5 |
| GND | GND, pin 9 (è un GND come il pin 6, ma sta sulla fila interna) |

**Distanza tra modulo e tag: almeno 5 mm, idealmente 6–8 mm, non oltre ~2 cm.** Alcuni tag (anche dello stesso modello degli altri) non rispondono quando sono troppo vicini all'antenna del PN532. Misurato su 7 statuette: 3 leggevano il 4–26% delle volte appoggiate a pochi mm dal modulo, e tutte il 99–100% con ~5 mm di spessore in più. Con l'ACR122U il problema non c'era, perché ha un'antenna diversa. La portata massima è circa 3–4 cm. Il materiale della zona di appoggio conta poco, ma vanno evitati i filamenti caricati con metallo o carbonio e le viti metalliche dentro la spira dell'antenna. Il LED rosso `PWR` del modulo è sempre acceso e non si può spegnere via software: se si vede attraverso il case, coprilo (nastro o smalto nero) o dissaldalo.

### LED di stato (opzionale)

LED da 5 mm con resistenza già integrata nel cavo, dimensionata per 5 V (es. Lumonic "LED con resistenza per 5V"). Pilotato a 3,3 V dal GPIO è già abbastanza luminoso, quindi non servono transistor né resistenze aggiuntive. Con un LED "nudo" va messa in serie una resistenza da 220–330 Ω.

```
pin 33 (GPIO13, PWM hardware) ── filo rosso (+)
pin 25 (GND)                  ── filo nero (−)
```

GPIO13 ha il PWM hardware, che rende l'effetto respiro fluido anche mentre VLC occupa la CPU (il PWM software tremola). All'accensione il pin è tenuto basso. `setup.sh` aggiunge `dtoverlay=pwm,pin=13,func=4` a `config.txt` (serve un riavvio).

Il LED è gestito da `led.py` (servizio `cucu-led.service`), che parte presto nel boot, indipendente da rete e VLC:

| Stato | LED |
|---|---|
| Avvio, oppure `read_nfc.py` fermo | respiro veloce |
| Idle, pausa, fine episodio | respiro lento |
| Video in corso, oppure visione bloccata dai limiti di tempo | fisso basso |

Luminosità e velocità si regolano con le costanti in cima a `led.py`. Sui dispositivi senza LED o senza PWM il servizio esce subito, senza errori.

`setup.sh` abilita il bus I2C a 100 kHz (serve un riavvio). Per verificare: `i2cdetect -y 1` deve mostrare `24`. Il formato degli UID è identico a quello dell'ACR122U, quindi le statuette già associate continuano a funzionare.

Un dispositivo v1 convertito a PN532 va aggiornato rieseguendo `sudo bash setup.sh`, perché l'aggiornamento OTA non abilita il bus I2C.

---

## Struttura del repository

```
cucu-device/
├── read_nfc.py                 # Script principale: NFC reader + VLC player
├── nfc_reader.py               # Backend lettore NFC (PN532 I2C / ACR122U)
├── led.py                      # LED di stato (PWM hardware, servizio cucu-led)
├── tags.json                   # Mapping UID NFC → personaggio (configurato via UI)
├── VERSION                     # Versione corrente (es. 0.1.0)
├── version.json                # Manifest OTA: versione remota + changelog
├── requirements.txt            # Dipendenze Python per l'API
├── bootstrap.sh                # One-shot installer (curl | sudo bash)
├── setup.sh                    # Script di installazione (idempotente)
├── updater.sh                  # Script aggiornamento OTA con fallback
├── setup_hotspot.sh            # Configurazione hotspot Wi-Fi di fallback
├── config.env.template         # Template configurazione OTA (copiare in config.env)
├── api/
│   ├── main.py                 # Server FastAPI (porta 80)
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
    ├── cucu-led.service        # LED di stato
    ├── splashscreen.service    # Splash screen al boot
    ├── cucu-device-updater.service
    └── cucu-device-updater.timer
```

---

## Installazione rapida

Su un Raspberry Pi con Raspberry Pi OS Lite 64-bit già avviato, un solo comando:

```bash
curl -sSL https://raw.githubusercontent.com/davidedori/cucu-device/main/bootstrap.sh | sudo bash
```

Questo comando scarica ed esegue `bootstrap.sh`, che installa `git` se mancante, clona il repository in `~/cucu-device` e lancia `setup.sh` in autonomia.

---

## Installazione manuale su Raspberry Pi vergine

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

Il dispositivo sarà raggiungibile su `http://cucu-XXXX.local`.

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
sudo uvicorn main:app --reload --host 0.0.0.0 --port 80
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
| `cucu-led.service` | boot (presto) | LED di stato (hardware v2) |
| `cucu-device-api.service` | boot | FastAPI su porta 80 |
| `splashscreen.service` | sysinit | Splash PNG su framebuffer |
| `cucu-device-updater.timer` | boot | Trigger OTA ogni notte alle 3:00 |

```bash
sudo systemctl status cucu-device.service
sudo journalctl -u cucu-device.service -f
```
