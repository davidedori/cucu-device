#!/bin/bash
# =============================================================================
# system_fixes.sh — sistemazioni di sistema che vanno fatte come root
#
# Lo esegue cucu-device.service prima di avviare il lettore (ExecStartPre con
# "+", quindi come root; con "-" un errore non ferma mai il lettore), a ogni
# avvio. Così una sistemazione arriva anche con un aggiornamento: l'OTA gira
# con lo updater.sh della versione PRECEDENTE (bash legge il file che aveva
# aperto), quindi quello che si aggiunge lì vale solo dall'aggiornamento dopo;
# il file di servizio nuovo invece lo installa, e poi riavvia il lettore.
#
# Ogni passo è idempotente e veloce: quando è già fatto non tocca niente.
# =============================================================================
set -u

# ---- 1. sudo non scrive nel log i comandi nmcli ------------------------------
# Collegando Cucù a una rete, l'API passa la password del Wi-Fi a nmcli tra gli
# argomenti (sudo nmcli con modify ... wifi-sec.psk ...), e sudo scrive ogni
# comando nel log di sistema, che è conservato tra un riavvio e l'altro. Gli
# altri comandi con sudo restano nel log; le righe dell'API ("[WiFi] ...") pure
NOLOG_FILE=/etc/sudoers.d/cucu-device-nolog
NOLOG_RULE='Defaults!/usr/bin/nmcli !log_allowed'
if [ "$(cat "$NOLOG_FILE" 2>/dev/null)" != "$NOLOG_RULE" ]; then
    tmp="$(mktemp)"
    echo "$NOLOG_RULE" > "$tmp"
    # una regola sbagliata bloccherebbe sudo: si installa solo se valida
    if visudo -c -q -f "$tmp" >/dev/null 2>&1 \
            && install -m 440 -o root -g root "$tmp" "$NOLOG_FILE"; then
        echo "sudo: i comandi nmcli non finiscono più nel log"
    else
        echo "sudo: regola per nmcli non installata"
    fi
    rm -f "$tmp"
fi

# ---- 2. Una volta sola: via il log scritto prima della regola ----------------
# Il log di sistema non si pulisce riga per riga: si cancella quello vecchio
# (conteneva le password del Wi-Fi). Solo dopo che la regola c'è
MARK=/var/lib/cucu-device/journal-cleaned-1
if [ -f "$NOLOG_FILE" ] && [ ! -f "$MARK" ]; then
    journalctl --rotate >/dev/null 2>&1
    journalctl --vacuum-time=1s >/dev/null 2>&1
    mkdir -p "$(dirname "$MARK")" && touch "$MARK"
    echo "Log di sistema precedente cancellato (conteneva password del Wi-Fi)"
fi

exit 0
