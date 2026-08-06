#!/bin/bash
# =============================================================================
# updater.sh — cucu-device OTA updater
#
# Eseguire come root (lanciato da cucu-device-updater.service via systemd timer).
#
# Flusso:
#   1. Legge config.env (REPO_URL, UPDATE_CHANNEL, AUTO_UPDATE)
#   2. Scarica version.json dal repo remoto
#   3. Confronta con VERSION locale (semver)
#   4. Se disponibile un aggiornamento:
#        a. Backup file configurazione utente (tags.json)
#        b. Salva hash commit attuale per rollback
#        c. git fetch + git reset --hard origin/<branch>
#        d. Ripristina file configurazione utente
#        e. Aggiorna dipendenze Python (venv)
#        f. Aggiorna file di servizio systemd
#        g. Riavvia i servizi
#        h. Health check dopo 10 secondi
#        i. Se health check fallisce → rollback al commit precedente + restart
# =============================================================================
set -euo pipefail

# ---- VARIABILI BASE ---------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
CONFIG_FILE="$PROJECT_DIR/config.env"
VERSION_FILE="$PROJECT_DIR/VERSION"
LOG_FILE="$PROJECT_DIR/logs/updater.log"
VENV_PIP="$PROJECT_DIR/api/venv/bin/pip"

# Rileva il proprietario della cartella progetto (per le operazioni git)
DEPLOY_USER="$(stat -c '%U' "$PROJECT_DIR" 2>/dev/null || echo 'davidedorigatti')"

# ---- LOG --------------------------------------------------------------------
mkdir -p "$(dirname "$LOG_FILE")"

_log() {
    local level="$1"; shift
    local msg="[$(date '+%Y-%m-%d %H:%M:%S')] [${level}] $*"
    echo "$msg" | tee -a "$LOG_FILE"
}
log()  { _log "INFO " "$@"; }
ok()   { _log "OK   " "$@"; }
warn() { _log "WARN " "$@"; }
fail() { _log "ERROR" "$@"; }

# Rotazione log: mantieni solo gli ultimi 500 KB
if [ -f "$LOG_FILE" ] && [ "$(stat -c%s "$LOG_FILE" 2>/dev/null || echo 0)" -gt 512000 ]; then
    mv "$LOG_FILE" "${LOG_FILE}.old"
fi

log "=================================================="
log "cucu-device OTA updater avviato"
log "=================================================="

# ---- VERIFICA ROOT ----------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    fail "Updater richiede privilegi root. Eseguire tramite systemd o con sudo."
    exit 1
fi

# ---- CARICA CONFIGURAZIONE --------------------------------------------------
if [ ! -f "$CONFIG_FILE" ]; then
    warn "config.env non trovato in $CONFIG_FILE"
    warn "Copia config.env.template in config.env e configura REPO_URL"
    exit 0
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

REPO_URL="${REPO_URL:-}"
UPDATE_CHANNEL="${UPDATE_CHANNEL:-stable}"
AUTO_UPDATE="${AUTO_UPDATE:-true}"

if [ "$AUTO_UPDATE" != "true" ]; then
    log "AUTO_UPDATE=false — aggiornamento saltato"
    exit 0
fi

if [ -z "$REPO_URL" ]; then
    fail "REPO_URL non configurato in config.env"
    exit 1
fi

# Mappa channel → branch git
case "$UPDATE_CHANNEL" in
    stable) GIT_BRANCH="main" ;;
    beta)   GIT_BRANCH="dev"  ;;
    *)
        warn "UPDATE_CHANNEL='$UPDATE_CHANNEL' non riconosciuto, uso 'stable'"
        GIT_BRANCH="main"
        ;;
esac

log "Config: REPO_URL=$REPO_URL | CHANNEL=$UPDATE_CHANNEL | BRANCH=$GIT_BRANCH"

# ---- VERSIONE LOCALE --------------------------------------------------------
LOCAL_VERSION="$(cat "$VERSION_FILE" 2>/dev/null | tr -d '[:space:]')"
if [ -z "$LOCAL_VERSION" ]; then
    warn "VERSION locale non trovata, procedo comunque"
    LOCAL_VERSION="0.0.0"
fi
log "Versione locale: $LOCAL_VERSION"

# ---- SCARICA VERSION.JSON ---------------------------------------------------
# Estrae "owner/repo" dall'URL GitHub (gestisce trailing slash e .git)
REPO_PATH="${REPO_URL%/}"
REPO_PATH="${REPO_PATH#*github.com/}"
REPO_PATH="${REPO_PATH%.git}"
VERSION_JSON_URL="https://raw.githubusercontent.com/${REPO_PATH}/${GIT_BRANCH}/version.json"

log "Scarico version.json da: $VERSION_JSON_URL"

VERSION_JSON="$(curl -fsSL --max-time 15 "$VERSION_JSON_URL" 2>/dev/null)" || {
    warn "Impossibile scaricare version.json (rete non disponibile?). Aggiornamento rimandato."
    exit 0
}

# Estrae "version" da JSON senza dipendenze esterne (jq potrebbe non esserci)
REMOTE_VERSION="$(echo "$VERSION_JSON" \
    | grep -o '"version"[[:space:]]*:[[:space:]]*"[^"]*"' \
    | grep -o '"[^"]*"$' \
    | tr -d '"')"

if [ -z "$REMOTE_VERSION" ]; then
    warn "version.json non valido o versione non leggibile. Aggiornamento rimandato."
    exit 0
fi

log "Versione remota: $REMOTE_VERSION"

# ---- CONFRONTO VERSIONI (semver) --------------------------------------------
# Usa sort -V: gestisce correttamente 1.0.9 < 1.0.10 < 1.1.0
version_gt() {
    # Ritorna 0 (true) se $1 > $2
    [ "$1" != "$2" ] && \
    [ "$(printf '%s\n' "$1" "$2" | sort -V | tail -1)" = "$1" ]
}

if ! version_gt "$REMOTE_VERSION" "$LOCAL_VERSION"; then
    ok "Versione $LOCAL_VERSION è aggiornata. Nessun aggiornamento necessario."
    exit 0
fi

log "Aggiornamento disponibile: $LOCAL_VERSION → $REMOTE_VERSION"

# ---- BACKUP FILE CONFIGURAZIONE UTENTE -------------------------------------
# tags.json e time_limits.json vengono modificati dall'utente (tramite UI web)
# e non devono essere sovrascritti dal git reset --hard. Vengono salvati prima
# e ripristinati dopo.
VOLATILE_FILES=("tags.json" "time_limits.json")
BACKUP_DIR="$(mktemp -d /tmp/cucu-ota-XXXXXX)"
trap 'rm -rf "$BACKUP_DIR"' EXIT

for f in "${VOLATILE_FILES[@]}"; do
    if [ -f "$PROJECT_DIR/$f" ]; then
        cp "$PROJECT_DIR/$f" "$BACKUP_DIR/$f"
        log "Backup: $f"
    fi
done

# ---- SALVA COMMIT CORRENTE PER ROLLBACK -------------------------------------
PREV_COMMIT="$(sudo -u "$DEPLOY_USER" git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || echo '')"
if [ -z "$PREV_COMMIT" ]; then
    fail "Impossibile leggere il commit attuale. Il progetto è un repository git?"
    exit 1
fi
log "Commit pre-update: $PREV_COMMIT"

# ---- FUNZIONE DI ROLLBACK ---------------------------------------------------
rollback() {
    fail "=================================================="
    fail "ROLLBACK al commit $PREV_COMMIT"
    fail "=================================================="

    sudo -u "$DEPLOY_USER" git -C "$PROJECT_DIR" reset --hard "$PREV_COMMIT" \
        2>>"$LOG_FILE" || fail "git reset --hard fallito durante rollback"

    ok "Codice ripristinato al commit $PREV_COMMIT"

    # Ripristina anche i file di servizio del vecchio commit
    _apply_service_files

    # Ripristina anche il tema Plymouth del vecchio commit
    _apply_plymouth_theme

    # Ripristina file utente
    _restore_volatile_files

    systemctl restart cucu-device.service     2>>"$LOG_FILE" || true
    systemctl restart cucu-device-api.service 2>>"$LOG_FILE" || true

    sleep 5

    if systemctl is-active --quiet cucu-device.service; then
        ok "Rollback riuscito. Servizio attivo."
    else
        fail "Rollback fallito. Intervento manuale necessario."
        fail "Stato: $(systemctl status cucu-device.service --no-pager -l 2>&1 | head -20)"
    fi
}

# ---- HELPER: AGGIORNA FILE SYSTEMD ------------------------------------------
_apply_service_files() {
    local changed=0
    for svc in cucu-device.service cucu-device-api.service splashscreen.service \
               cucu-device-updater.service cucu-device-updater.timer; do
        if [ -f "$PROJECT_DIR/systemd/$svc" ]; then
            local _uid
            _uid="$(id -u "$DEPLOY_USER")"
            sed \
                -e "s|/home/davidedorigatti/|/home/${DEPLOY_USER}/|g" \
                -e "s|User=davidedorigatti|User=${DEPLOY_USER}|g" \
                -e "s|__DEPLOY_UID__|${_uid}|g" \
                "$PROJECT_DIR/systemd/$svc" > "/etc/systemd/system/$svc"
            changed=1
        fi
    done
    if [ "$changed" -eq 1 ]; then
        systemctl daemon-reload
        ok "File di servizio systemd aggiornati"
    fi
}

# ---- HELPER: SINCRONIZZA TEMA PLYMOUTH --------------------------------------
# setup.sh copia plymouth/*.png + cucu.script in /usr/share/plymouth/themes/cucu
# e rigenera l'initramfs, ma setup.sh non viene rieseguito dall'OTA automatico.
# Senza questo passaggio, un aggiornamento che cambia i frame di boot lascia il
# tema installato (e l'initramfs) disallineato dal repo: al boot successivo
# Plymouth cerca frame che non corrispondono più a quelli attesi dallo script.
_apply_plymouth_theme() {
    local theme_dir="/usr/share/plymouth/themes/cucu"
    if [ ! -d "$PROJECT_DIR/plymouth" ]; then
        return
    fi
    mkdir -p "$theme_dir"
    cp "$PROJECT_DIR/plymouth/cucu.plymouth" "$theme_dir/" 2>>"$LOG_FILE" || true
    cp "$PROJECT_DIR/plymouth/cucu.script"   "$theme_dir/" 2>>"$LOG_FILE" || true
    # Rimuove i frame vecchi prima di ricopiare: evita che un cambio nel numero
    # di frame lasci file orfani non referenziati (o mancanti) nel tema attivo.
    rm -f "$theme_dir"/boot_*.png
    cp "$PROJECT_DIR/plymouth/"*.png "$theme_dir/" 2>>"$LOG_FILE" || true
    if [ -f "$PROJECT_DIR/graphics/splash.png" ]; then
        cp "$PROJECT_DIR/graphics/splash.png" "$theme_dir/" 2>>"$LOG_FILE" || true
    fi
    ok "Tema Plymouth risincronizzato in $theme_dir"

    # setup.sh imposta "cucu" come tema di default alla prima installazione, ma
    # l'OTA non richiama setup.sh: su un dispositivo aggiornato via OTA da una
    # versione pre-Plymouth (o con il tema resettato da un upgrade di pacchetti
    # di sistema) il tema attivo resterebbe quello di default di Debian, con i
    # frame cucu sincronizzati su disco ma mai mostrati al boot. Idempotente.
    if plymouth-set-default-theme cucu 2>>"$LOG_FILE"; then
        ok "Tema Plymouth impostato come default: cucu"
    else
        warn "plymouth-set-default-theme fallito — il tema cucu potrebbe non essere attivo al prossimo boot"
    fi

    # DeviceTimeout di default (8s) e' troppo corto su questo hardware: vc4-drm
    # a volte non e' ancora "inizializzato" secondo udev quando scade, e
    # Plymouth rinuncia al tema grafico per il fallback testuale (confermato
    # via log plymouth.debug). Stesso fix di setup.sh, retrofit sui device
    # esistenti che non lo rieseguono.
    #
    # L'inserimento DEVE andare dopo la riga "Theme=", non prima: l'hook
    # initramfs-tools di plymouth chiama `plymouth-set-default-theme` senza
    # argomenti per decidere quale tema includere, e quello script legge il
    # tema con un awk che considera solo il PRIMO campo della sezione
    # [Daemon] (assume sia "Theme="). Se DeviceTimeout finisce prima, quel
    # parser ritorna vuoto e l'hook impacchetta "text" invece di "cucu"
    # nell'initramfs — verificato dal vivo, non e' teorico.
    local plymouthd_conf="/etc/plymouth/plymouthd.conf"
    if grep -q '^DeviceTimeout' "$plymouthd_conf" 2>/dev/null; then
        sed -i -E 's/^DeviceTimeout[[:blank:]]*=.*/DeviceTimeout=20/' "$plymouthd_conf" 2>>"$LOG_FILE" || true
    else
        sed -i -e '/^Theme=/a DeviceTimeout=20' "$plymouthd_conf" 2>>"$LOG_FILE" || true
    fi
    ok "plymouthd.conf: DeviceTimeout impostato a 20s"

    # /boot/firmware (FAT32, non journalizzata) e' montata read-only fuori da
    # questa finestra (vedi setup.sh) apposta per non lasciarla "dirty" se il
    # dispositivo perde corrente: un dirty bit su questa partizione fa leggere
    # al bootloader (che gira prima di Linux/fsck, con un suo driver FAT che
    # ignora il dirty bit) un initramfs incoerente al boot successivo, con
    # schermata grigia al posto dell'animazione. Riportarla rw solo per la
    # scrittura riduce la finestra di rischio da "sempre" a "pochi secondi a
    # notte".
    mount -o remount,rw /boot/firmware 2>>"$LOG_FILE" || warn "remount rw di /boot/firmware fallito"

    log "Rigenerazione initramfs (30-60s)..."
    if update-initramfs -u 2>>"$LOG_FILE"; then
        ok "initramfs aggiornato"
    else
        warn "update-initramfs fallito — il tema Plymouth potrebbe non essere aggiornato al prossimo boot"
    fi

    mount -o remount,ro /boot/firmware 2>>"$LOG_FILE" || warn "remount ro di /boot/firmware fallito"
}

# ---- HELPER: RIPRISTINA FILE UTENTE -----------------------------------------
_restore_volatile_files() {
    for f in "${VOLATILE_FILES[@]}"; do
        if [ -f "$BACKUP_DIR/$f" ]; then
            cp "$BACKUP_DIR/$f" "$PROJECT_DIR/$f"
            log "Ripristinato: $f"
        fi
    done
}

# ---- GIT FETCH + RESET ------------------------------------------------------
log "Scarico aggiornamenti dal repository..."

if ! sudo -u "$DEPLOY_USER" git -C "$PROJECT_DIR" fetch origin "$GIT_BRANCH" \
        2>>"$LOG_FILE"; then
    fail "git fetch fallito. Aggiornamento annullato."
    exit 1
fi

if ! sudo -u "$DEPLOY_USER" git -C "$PROJECT_DIR" \
        reset --hard "origin/$GIT_BRANCH" 2>>"$LOG_FILE"; then
    fail "git reset --hard fallito. Aggiornamento annullato."
    exit 1
fi

ok "Codice aggiornato a $REMOTE_VERSION (commit: $(sudo -u "$DEPLOY_USER" git -C "$PROJECT_DIR" rev-parse --short HEAD))"

# ---- RIPRISTINA FILE CONFIGURAZIONE UTENTE ----------------------------------
_restore_volatile_files

# ---- AGGIORNA DIPENDENZE PYTHON ---------------------------------------------
log "Verifica dipendenze Python..."
if [ -f "$VENV_PIP" ]; then
    sudo -u "$DEPLOY_USER" "$VENV_PIP" install \
        -r "$PROJECT_DIR/requirements.txt" -q 2>>"$LOG_FILE"
    ok "Dipendenze Python aggiornate"
else
    warn "venv non trovato in $PROJECT_DIR/api/venv — eseguire setup.sh per inizializzare"
fi

# ---- AGGIORNA FILE DI SERVIZIO SYSTEMD -------------------------------------
_apply_service_files

# ---- SINCRONIZZA TEMA PLYMOUTH ----------------------------------------------
_apply_plymouth_theme

# ---- RIAVVIA SERVIZI --------------------------------------------------------
log "Riavvio servizi..."
systemctl restart cucu-device.service     2>>"$LOG_FILE" || true
systemctl restart cucu-device-api.service 2>>"$LOG_FILE" || true

# Attende la stabilizzazione
log "Attesa stabilizzazione servizi (10s)..."
sleep 10

# ---- HEALTH CHECK -----------------------------------------------------------
FAILED=()
for svc in cucu-device.service cucu-device-api.service; do
    if systemctl is-active --quiet "$svc"; then
        ok "Servizio attivo: $svc"
    else
        fail "Servizio NON attivo dopo aggiornamento: $svc"
        fail "$(systemctl status "$svc" --no-pager -l 2>&1 | head -15)"
        FAILED+=("$svc")
    fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
    fail "Health check fallito (${FAILED[*]}). Avvio rollback..."
    rollback
    exit 1
fi

# ---- COMPLETATO -------------------------------------------------------------
ok "=================================================="
ok "Aggiornamento completato: $LOCAL_VERSION → $REMOTE_VERSION"
ok "=================================================="
exit 0
