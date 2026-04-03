#!/bin/bash
# setup_hotspot.sh — configura l'hotspot Wi-Fi di fallback per cucu-device
# Usato da setup.sh come step di configurazione e può essere eseguito standalone.
# L'hotspot si attiva automaticamente quando nessuna rete nota è disponibile.
#
# SSID e nome connessione NM vengono derivati dall'hostname del dispositivo
# (es. hostname=cucu-4aab → SSID=cucu-4aab_AP, connessione NM=cucu-4aab_AP)

# Configurazione
HOTSPOT_SSID="$(hostname)_AP"
HOTSPOT_PASSWORD="cucusetup"
CONNECTION_NAME="$(hostname)_AP"

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

echo "Hotspot configurato: SSID=$HOTSPOT_SSID | Password=$HOTSPOT_PASSWORD"
