#!/bin/bash
# =============================================================================
# bootstrap.sh — cucu-device one-shot installer
#
# Uso:
#   curl -sSL https://raw.githubusercontent.com/davidedori/cucu-device/main/bootstrap.sh | sudo bash
#
# Cosa fa:
#   1. Verifica che git sia installato (lo installa se manca)
#   2. Rileva l'utente non-root corretto (chi ha invocato sudo)
#   3. Clona il repository in ~/cucu-device (saltato se esiste già)
#   4. Esegue setup.sh
# =============================================================================
set -euo pipefail

# ---- COLORI -----------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC}  $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $1"; }
fail() { echo -e "  ${RED}✗${NC}  $1"; exit 1; }

REPO_URL="https://github.com/davidedori/cucu-device.git"
BRANCH="main"

# ---- VERIFICA ROOT ----------------------------------------------------------
if [ "$EUID" -ne 0 ]; then
    fail "bootstrap.sh richiede privilegi root. Esegui con: sudo bash bootstrap.sh"
fi

# ---- RILEVA UTENTE ----------------------------------------------------------
if [ -n "${SUDO_USER:-}" ]; then
    DEPLOY_USER="$SUDO_USER"
else
    # Eseguito come root puro (es. pipe da curl su sistema senza utente normale)
    # Prova a trovare il primo utente non-root con home reale
    DEPLOY_USER="$(getent passwd | awk -F: '$3 >= 1000 && $7 !~ /nologin|false/ {print $1; exit}')"
    if [ -z "$DEPLOY_USER" ]; then
        fail "Impossibile rilevare l'utente. Esegui con: sudo bash bootstrap.sh da un utente normale."
    fi
    warn "SUDO_USER non impostato, uso utente rilevato automaticamente: $DEPLOY_USER"
fi

DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
TARGET_DIR="${DEPLOY_HOME}/cucu-device"

echo -e "\n${BLUE}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║       cucu-device  bootstrap            ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Utente:  $DEPLOY_USER"
echo "  Target:  $TARGET_DIR"
echo "  Repo:    $REPO_URL"
echo ""

# ---- STEP 1: GIT ------------------------------------------------------------
echo -e "${BLUE}${BOLD}[1]${NC} Verifica git"

if ! command -v git &>/dev/null; then
    warn "git non trovato, installazione in corso..."
    apt-get update -y -q
    apt-get install -y -q git
    ok "git installato: $(git --version)"
else
    ok "git già presente: $(git --version)"
fi

# ---- STEP 2: CLONE ----------------------------------------------------------
echo -e "\n${BLUE}${BOLD}[2]${NC} Clone repository"

if [ -d "$TARGET_DIR/.git" ]; then
    ok "Repository già presente in $TARGET_DIR, aggiornamento..."
    sudo -u "$DEPLOY_USER" git -C "$TARGET_DIR" fetch origin "$BRANCH" 2>/dev/null
    sudo -u "$DEPLOY_USER" git -C "$TARGET_DIR" reset --hard "origin/$BRANCH" 2>/dev/null
    ok "Repository aggiornato al branch $BRANCH"
elif [ -d "$TARGET_DIR" ] && [ "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
    fail "$TARGET_DIR esiste ma non è un repository git. Rimuovilo manualmente e riprova."
else
    sudo -u "$DEPLOY_USER" git clone --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR"
    ok "Repository clonato in $TARGET_DIR"
fi

# ---- STEP 3: SETUP ----------------------------------------------------------
echo -e "\n${BLUE}${BOLD}[3]${NC} Esecuzione setup.sh"
echo ""

SETUP_SCRIPT="$TARGET_DIR/setup.sh"

if [ ! -f "$SETUP_SCRIPT" ]; then
    fail "setup.sh non trovato in $TARGET_DIR. Il clone è andato a buon fine?"
fi

chmod +x "$SETUP_SCRIPT"
bash "$SETUP_SCRIPT"
