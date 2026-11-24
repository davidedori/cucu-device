#!/bin/bash
# =============================================================================
# wifi_reset.sh — ricarica il driver del Wi-Fi (brcmfmac) del Raspberry Pi
#
# system_fixes.sh lo installa come /usr/local/sbin/cucu-wifi-reset (di root, non
# modificabile dall'utente) e una regola sudo lascia che l'API lo lanci senza
# password. Lo usa _try_saved_networks in api/main.py quando, dopo una caduta o
# dopo l'hotspot, il chip non riesce più a cercare le reti ("Failed to initiate
# sched scan"): spegnere e riaccendere la radio non basta, ricaricare il driver sì.
# Esce 0 quando wlan0 è tornata.
# =============================================================================
set -u

# brcmfmac_wcc (kernel recenti) usa brcmfmac: va tolto per primo
modprobe -r brcmfmac_wcc 2>/dev/null
if ! modprobe -r brcmfmac; then
    echo "brcmfmac non si toglie" >&2
    exit 1
fi
sleep 2
if ! modprobe brcmfmac; then
    echo "brcmfmac non si carica" >&2
    exit 1
fi
for _ in $(seq 1 20); do
    [ -e /sys/class/net/wlan0 ] && exit 0
    sleep 1
done
echo "wlan0 non è tornata" >&2
exit 1
