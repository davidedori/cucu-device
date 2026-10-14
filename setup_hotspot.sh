#!/bin/bash
# setup_hotspot.sh — configura l'hotspot Wi-Fi di fallback per cucu-device
# Usato da setup.sh come step di configurazione e può essere eseguito standalone.
# L'hotspot si attiva automaticamente quando nessuna rete nota è disponibile.
#
# SSID e nome connessione NM vengono derivati dall'hostname del dispositivo
# (es. hostname=cucu-4aab → SSID=cucu-4aab_AP, connessione NM=cucu-4aab_AP)

#
# Password: una per dispositivo, casuale, in config.env (HOTSPOT_PASSWORD, fuori da
# git). Se manca la si genera, poi non cambia più: la ritrova chi vede la TV
# ("Colleghiamoci!" e il gesto d'emergenza) e l'app, in Impostazioni → Wi-Fi.
# Prima era "cucusetup" per tutti, scritta nel repository pubblico.
#
#   bash setup_hotspot.sh                  crea (o ricrea) l'hotspot
#   bash setup_hotspot.sh --password-only  solo la password: la genera se manca e la
#                                          imposta sull'hotspot che c'è (dall'OTA)
# CUCU_CONFIG_ENV: dove sta config.env (di base accanto allo script)

# Configurazione
HOTSPOT_SSID="$(hostname)_AP"
CONNECTION_NAME="$(hostname)_AP"
CONFIG_ENV="${CUCU_CONFIG_ENV:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.env}"

# Lettere minuscole e cifre senza quelle che si confondono (l/1, o/0, i): quando non
# si può inquadrare il QR la password si legge dalla TV e si digita. 12 caratteri,
# circa 60 bit, in tre gruppi
_new_password() {
    python3 -c "import secrets; a = 'abcdefghjkmnpqrstuvwxyz23456789'; print('-'.join(''.join(secrets.choice(a) for _ in range(4)) for _ in range(3)))"
}

HOTSPOT_PASSWORD="$(grep -E '^HOTSPOT_PASSWORD=' "$CONFIG_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"[:space:]')"
if [ -z "$HOTSPOT_PASSWORD" ]; then
    HOTSPOT_PASSWORD="$(_new_password)"
    if [ -z "$HOTSPOT_PASSWORD" ]; then
        echo "Errore: password dell'hotspot non generata (manca python3?)."
        exit 1
    fi
    touch "$CONFIG_ENV"
    printf "\n# Password dell'hotspot (rete %s), generata alla prima installazione: non cambiarla\n# a mano senza rilanciare setup_hotspot.sh\nHOTSPOT_PASSWORD=%s\n" \
        "$HOTSPOT_SSID" "$HOTSPOT_PASSWORD" >> "$CONFIG_ENV"
    echo "Password dell'hotspot generata e salvata in $CONFIG_ENV"
fi
chmod 600 "$CONFIG_ENV"   # contiene una password: la legge solo il proprietario (l'API gira come lui)

if [ "${1:-}" = "--password-only" ]; then
    if nmcli -t -f NAME con show 2>/dev/null | grep -qx "$CONNECTION_NAME"; then
        nmcli con modify "$CONNECTION_NAME" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$HOTSPOT_PASSWORD"
        echo "Password dell'hotspot impostata su $CONNECTION_NAME"
    fi
    exit 0
fi

echo "Configurazione hotspot Wi-Fi di fallback: $HOTSPOT_SSID"

# Verifica nmcli
if ! command -v nmcli &> /dev/null; then
    echo "Errore: nmcli (NetworkManager) non trovato."
    echo "Assicurati che network-manager sia installato (apt install network-manager)."
    exit 1
fi

# Rimuove eventuali connessioni hotspot esistenti (gestisce anche cambi hostname)
# Cerca tutte le connessioni Wi-Fi con nome che termina in _AP
while IFS= read -r old_conn; do
    [[ "$old_conn" == *"_AP" ]] || continue
    echo "Rimozione connessione hotspot esistente: $old_conn"
    nmcli connection delete "$old_conn" 2>/dev/null || true
done < <(nmcli -t -f NAME connection show 2>/dev/null)

# Crea la connessione hotspot
echo "Creazione connessione hotspot: $CONNECTION_NAME..."
nmcli con add type wifi ifname wlan0 con-name "$CONNECTION_NAME" \
    autoconnect yes \
    ssid "$HOTSPOT_SSID"

# Configura: AP mode, banda 2.4GHz, DHCP condiviso, WPA2-PSK
# autoconnect-priority -1: attivato solo se nessuna rete con priorità >= 0 è disponibile
nmcli con modify "$CONNECTION_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$HOTSPOT_PASSWORD" \
    connection.autoconnect-priority -1

echo "Hotspot configurato: SSID=$HOTSPOT_SSID | Password=$HOTSPOT_PASSWORD (in $CONFIG_ENV)"
