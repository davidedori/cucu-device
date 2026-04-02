#!/bin/bash

# Configuration
HOTSPOT_SSID="CucuDevice_AP"
HOTSPOT_PASSWORD="cucu-device"
CONNECTION_NAME="Hotspot"

echo "Configuring Auto-Hotspot for cucu-device..."

# Check for nmcli
if ! command -v nmcli &> /dev/null; then
    echo "Error: nmcli (NetworkManager) is not installed or not found."
    echo "Please ensure you are running a recent Raspberry Pi OS (Bookworm) or have enabled NetworkManager."
    exit 1
fi

# Delete existing connection if it exists to ensure clean state
if nmcli connection show "$CONNECTION_NAME" &> /dev/null; then
    echo "Removing existing '$CONNECTION_NAME' connection..."
    sudo nmcli connection delete "$CONNECTION_NAME"
fi

# Create the Hotspot connection
echo "Creating new Hotspot connection..."
# ipv4.method shared -> creates a Hotspot with DHCP
# connection.autoconnect yes -> enable autoconnect
# connection.autoconnect-priority 5 -> lower priority than standard connections (usually 100 or 0)

sudo nmcli con add type wifi ifname wlan0 con-name "$CONNECTION_NAME" \
    autoconnect yes \
    ssid "$HOTSPOT_SSID"

# Modify settings
echo "Setting security and parameters..."
sudo nmcli con modify "$CONNECTION_NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$HOTSPOT_PASSWORD" \
    connection.autoconnect-priority 5

echo "Done! The Hotspot '$HOTSPOT_SSID' has been configured."
echo "If no known Wi-Fi networks are found, the Raspberry Pi will create this network."
echo "Password: $HOTSPOT_PASSWORD"
