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

# ---- 3. Wi-Fi: chi è l'utente del servizio -----------------------------------
# system_fixes.sh sta in /home/<utente>/cucu-device: l'utente è il proprietario
HERE="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_USER="$(stat -c %U "$HERE" 2>/dev/null)"

# ---- 4. Driver del Wi-Fi ricaricabile dall'API ------------------------------
# Dopo una caduta o dopo l'hotspot il chip del Pi Zero 2 W a volte non cerca più
# le reti e Cucù non ritrova la rete di casa (cucu-4aab, 24/11/2026): l'API
# ricarica il driver con cucu-wifi-reset. Copiato fuori dalla cartella
# dell'utente, di root, perché lo si lancia con sudo
RESET_SRC="$HERE/wifi_reset.sh"
RESET_DST=/usr/local/sbin/cucu-wifi-reset
if [ -f "$RESET_SRC" ] && ! cmp -s "$RESET_SRC" "$RESET_DST"; then
    install -m 755 -o root -g root "$RESET_SRC" "$RESET_DST" \
        && echo "Wi-Fi: installato $RESET_DST"
fi

# Regole sudo per l'API: spegnere l'hotspot e ricaricare il driver. Le altre
# (con up, add, modify, delete) sono in /etc/sudoers.d/cucu-device (setup.sh)
WIFI_SUDO=/etc/sudoers.d/cucu-device-wifi
if [ -n "$DEPLOY_USER" ] && [ "$DEPLOY_USER" != "root" ]; then
    rule="$DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/nmcli con down *, $RESET_DST"
    if [ "$(cat "$WIFI_SUDO" 2>/dev/null)" != "$rule" ]; then
        tmp="$(mktemp)"
        echo "$rule" > "$tmp"
        if visudo -c -q -f "$tmp" >/dev/null 2>&1 \
                && install -m 440 -o root -g root "$tmp" "$WIFI_SUDO"; then
            echo "sudo: l'API può spegnere l'hotspot e ricaricare il driver del Wi-Fi"
        else
            echo "sudo: regola per il Wi-Fi non installata"
        fi
        rm -f "$tmp"
    fi
fi

# ---- 5. Wi-Fi senza risparmio energetico -------------------------------------
# Con il risparmio energetico il chip si addormenta: il router lo considera
# inattivo e lo butta fuori (reason=4), e l'indirizzo arriva dopo 40 secondi.
# Vale per tutte le reti che non dicono altro (powersave 0 = predefinito)
PS_FILE=/etc/NetworkManager/conf.d/50-cucu-wifi.conf
PS_CONF="$(printf '[connection]\nwifi.powersave=2\n')"
if [ "$(cat "$PS_FILE" 2>/dev/null)" != "$PS_CONF" ]; then
    mkdir -p "$(dirname "$PS_FILE")"
    printf '%s\n' "$PS_CONF" > "$PS_FILE"
    # vale dalla prossima connessione, senza far cadere quella di adesso
    nmcli general reload conf >/dev/null 2>&1 || systemctl reload NetworkManager >/dev/null 2>&1
    echo "Wi-Fi: risparmio energetico spento"
fi

# ---- 6. Hotspot solo WPA2 (CCMP) ---------------------------------------------
# Senza indicarlo l'hotspot si presentava WPA1/TKIP mischiato a WPA2: alcuni
# dispositivi non riuscivano a collegarsi. Vale dalla prossima accensione
AP="$(hostname)_AP"
if nmcli -t -f NAME con show 2>/dev/null | grep -qx "$AP" \
        && [ "$(nmcli -g 802-11-wireless-security.proto con show "$AP" 2>/dev/null)" != "rsn" ]; then
    nmcli con modify "$AP" 802-11-wireless-security.proto rsn \
        802-11-wireless-security.pairwise ccmp 802-11-wireless-security.group ccmp \
        && echo "Hotspot: solo WPA2"
fi

exit 0
