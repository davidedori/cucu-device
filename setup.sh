#!/bin/bash
# =============================================================================
# setup.sh — cucu-device setup script
# Eseguire con: sudo bash setup.sh
#
# Idempotente: può essere eseguito più volte senza effetti collaterali.
# Compatibile con Raspberry Pi OS Lite 64-bit (Debian bookworm/trixie).
# =============================================================================
set -euo pipefail

# ---- COLORI E LOG -----------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
STEP=0

step() { STEP=$((STEP+1)); echo -e "\n${BLUE}${BOLD}[$STEP]${NC} $1"; }
ok()   { echo -e "    ${GREEN}✓${NC}  $1"; }
warn() { echo -e "    ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "    ${RED}✗${NC}  $1"; exit 1; }

# ---- VARIABILI --------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$REPO_DIR/VERSION" 2>/dev/null || echo "unknown")"

# Rileva l'utente che ha invocato sudo (il proprietario effettivo del progetto)
if [ -n "${SUDO_USER:-}" ]; then
    DEPLOY_USER="$SUDO_USER"
elif id "davidedorigatti" &>/dev/null; then
    DEPLOY_USER="davidedorigatti"
else
    fail "Impossibile rilevare l'utente. Esegui con: sudo bash setup.sh"
fi

PROJECT_DIR="/home/${DEPLOY_USER}/cucu-device"
VENV_DIR="${PROJECT_DIR}/api/venv"
SUDOERS_FILE="/etc/sudoers.d/cucu-device"

# ---- VERIFICA ROOT ----------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    fail "Questo script richiede privilegi root. Esegui con: sudo bash setup.sh"
fi

# ---- BANNER -----------------------------------------------------------------
echo -e "\n${BLUE}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║       cucu-device  setup  v${VERSION}          ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Utente:   $DEPLOY_USER"
echo "  Progetto: $PROJECT_DIR"
echo "  Repo:     $REPO_DIR"

# =============================================================================
# STEP 1 — HOSTNAME UNIVOCO DA MAC ADDRESS
# =============================================================================
step "Configurazione hostname univoco (MAC-based)"

# Legge il MAC di wlan0, fallback su eth0, fallback su suffisso casuale
MAC_RAW=""
for iface in wlan0 eth0 wlan1; do
    if MAC_TMP=$(cat "/sys/class/net/${iface}/address" 2>/dev/null); then
        MAC_RAW="${MAC_TMP//:}"   # rimuove i due punti
        ok "MAC rilevato da ${iface}: ${MAC_TMP}"
        break
    fi
done

if [ -z "$MAC_RAW" ]; then
    warn "Nessuna interfaccia di rete rilevata, uso suffisso casuale"
    MAC_RAW="0000$(tr -dc 'a-f0-9' </dev/urandom | head -c 4)"
fi

# Ultimi 4 caratteri esadecimali del MAC (es. dc:a6:32:a3:f2:b1 → f2b1)
SUFFIX="${MAC_RAW: -4}"
NEW_HOSTNAME="cucu-${SUFFIX}"
CURRENT_HOSTNAME=$(hostname)

if [ "$CURRENT_HOSTNAME" != "$NEW_HOSTNAME" ]; then
    hostnamectl set-hostname "$NEW_HOSTNAME"

    # Aggiorna /etc/hosts: rimuove righe 127.0.1.1 esistenti e le ricrea
    sed -i '/^127\.0\.1\.1/d' /etc/hosts
    printf '127.0.1.1\t%s\n' "$NEW_HOSTNAME" >> /etc/hosts

    ok "Hostname impostato: ${BOLD}${NEW_HOSTNAME}${NC}"
    warn "Il nuovo hostname sarà attivo al prossimo riavvio"
else
    ok "Hostname già corretto: ${BOLD}${NEW_HOSTNAME}${NC}"
fi

# =============================================================================
# STEP 2 — AGGIORNAMENTO SISTEMA
# =============================================================================
step "Aggiornamento sistema (apt update / upgrade)"

apt-get update -y -q
# --force-confold: mantiene i file di configurazione esistenti senza chiedere
apt-get upgrade -y -q \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold"

ok "Sistema aggiornato"

# =============================================================================
# STEP 3 — DIPENDENZE DI SISTEMA
# =============================================================================
step "Installazione dipendenze di sistema"

APT_PACKAGES=(
    python3-vlc        # binding Python per VLC (usato da read_nfc.py)
    vlc-bin            # core binaries VLC
    vlc-plugin-base    # plugin base VLC
    libnfc-bin         # nfc-list (usato da read_nfc.py per leggere i tag)
    fbi                # framebuffer image viewer (splash screen)
    python3-venv       # per creare il venv dell'API
    python3-pip        # pip
    git                # gestione aggiornamenti
    curl               # fetch version.json per OTA
    network-manager    # nmcli (gestione Wi-Fi dall'API)
    avahi-daemon       # mDNS: rende raggiungibile <hostname>.local
)

apt-get install -y -q "${APT_PACKAGES[@]}"
ok "Pacchetti installati: ${APT_PACKAGES[*]}"

# =============================================================================
# STEP 4 — STRUTTURA CARTELLE
# =============================================================================
step "Creazione struttura cartelle"

DIRS=(
    "$PROJECT_DIR"
    "$PROJECT_DIR/api"
    "$PROJECT_DIR/characters"
    "$PROJECT_DIR/graphics"
    "$PROJECT_DIR/logs"
)

for dir in "${DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        ok "Creata: $dir"
    else
        ok "Già presente: $dir"
    fi
done

chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "$PROJECT_DIR"

# =============================================================================
# STEP 5 — COPIA FILE DI PROGETTO
# =============================================================================
step "Copia file di progetto"

# Codice: sovrascrive sempre (è la fonte di verità)
cp "$REPO_DIR/read_nfc.py"      "$PROJECT_DIR/read_nfc.py"
cp "$REPO_DIR/api/main.py"      "$PROJECT_DIR/api/main.py"
cp "$REPO_DIR/api/index.html"   "$PROJECT_DIR/api/index.html"
cp "$REPO_DIR/VERSION"          "$PROJECT_DIR/VERSION"
ok "Copiati: read_nfc.py, api/main.py, api/index.html, VERSION"

# Grafica: sovrascrive sempre
cp -r "$REPO_DIR/graphics/"* "$PROJECT_DIR/graphics/"
ok "Copiata: graphics/"

# tags.json: preserva la configurazione esistente (tag NFC associati)
if [ ! -f "$PROJECT_DIR/tags.json" ]; then
    cp "$REPO_DIR/tags.json" "$PROJECT_DIR/tags.json"
    ok "Copiato: tags.json (primo avvio)"
else
    ok "Mantenuto: tags.json (configurazione esistente preservata)"
fi

chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "$PROJECT_DIR"

# =============================================================================
# STEP 6 — AMBIENTE PYTHON (VENV API)
# =============================================================================
step "Configurazione ambiente Python per l'API"

if [ ! -d "$VENV_DIR" ]; then
    sudo -u "$DEPLOY_USER" python3 -m venv "$VENV_DIR"
    ok "Venv creato: $VENV_DIR"
else
    ok "Venv già presente, aggiornamento dipendenze"
fi

sudo -u "$DEPLOY_USER" "$VENV_DIR/bin/pip" install --upgrade pip -q
sudo -u "$DEPLOY_USER" "$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt" -q
ok "Dipendenze Python installate (da requirements.txt)"

# =============================================================================
# STEP 7 — SUDOERS
# =============================================================================
step "Configurazione sudoers per operazioni di sistema"

# L'API ha bisogno di eseguire comandi con sudo senza password interattiva:
# - riavvio del servizio NFC reader
# - gestione connessioni Wi-Fi tramite nmcli
cat > "$SUDOERS_FILE" <<SUDOERS_EOF
# cucu-device: permessi sudo passwordless per operazioni di sistema
# Generato da setup.sh — non modificare manualmente

# Gestione servizio principale
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /bin/systemctl restart cucu-device.service
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /bin/systemctl stop cucu-device.service
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /bin/systemctl start cucu-device.service

# Gestione Wi-Fi tramite nmcli (usato dall'API web)
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /usr/bin/nmcli con delete *
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /usr/bin/nmcli con add *
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /usr/bin/nmcli con modify *
${DEPLOY_USER} ALL=(ALL) NOPASSWD: /usr/bin/nmcli con up *
SUDOERS_EOF

chmod 440 "$SUDOERS_FILE"

# Verifica sintassi sudoers prima di andare avanti
if visudo -c -f "$SUDOERS_FILE" &>/dev/null; then
    ok "Sudoers configurato e validato: $SUDOERS_FILE"
else
    rm -f "$SUDOERS_FILE"
    fail "Errore nella sintassi del file sudoers. File rimosso."
fi

# =============================================================================
# STEP 8 — SERVIZI SYSTEMD
# =============================================================================
step "Configurazione e abilitazione servizi systemd"

SERVICES=(cucu-device.service cucu-device-api.service splashscreen.service)

for svc in "${SERVICES[@]}"; do
    cp "$REPO_DIR/systemd/$svc" "/etc/systemd/system/$svc"
    ok "Installato: $svc"
done

systemctl daemon-reload
ok "daemon-reload eseguito"

for svc in "${SERVICES[@]}"; do
    systemctl enable "$svc"
    ok "Abilitato all'avvio: $svc"
done

# =============================================================================
# RIEPILOGO
# =============================================================================
echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║             cucu-device setup completato!               ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
printf "  %-14s %s\n" "Hostname:"  "${NEW_HOSTNAME}.local"
printf "  %-14s %s\n" "Progetto:"  "$PROJECT_DIR"
printf "  %-14s %s\n" "API web:"   "http://${NEW_HOSTNAME}.local:8000"
printf "  %-14s %s\n" "Versione:"  "$VERSION"
echo ""
echo -e "  ${BOLD}Prossimi passi:${NC}"
echo "   1. Aggiungi i video dei personaggi in:"
echo "      ${PROJECT_DIR}/characters/<nome_personaggio>/"
echo ""
echo "   2. Configura Wi-Fi e associa i tag NFC dall'interfaccia web:"
echo "      http://${NEW_HOSTNAME}.local:8000"
echo ""
echo "   3. Riavvia il dispositivo per attivare tutti i servizi:"
echo "      sudo reboot"
echo ""
