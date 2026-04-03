# 🛠 Cucu-Device Cheatsheet

Questo file raccoglie tutti i comandi utili da terminale (SSH) per installare, aggiornare, fare manutenzione e risolvere problemi sul Raspberry Pi.

---

## 🚀 1. Installazione e Aggiornamento

**Nuova Installazione da zero (su RPi OS Lite vergine):**
```bash
curl -sSL https://raw.githubusercontent.com/davidedori/cucu-device/main/bootstrap.sh | sudo bash
```

**Aggiornare manualmente un dispositivo esistente all'ultima versione (tutto in uno):**
```bash
cd ~/cucu-device && git pull origin main && sudo bash setup.sh && sudo reboot
```

**Riavviare i servizi dopo un aggiornamento minore (senza rilanciare setup.sh):**
```bash
sudo systemctl restart cucu-device-api.service
sudo systemctl restart cucu-device.service
```

---

## 📦 2. Pre-Spedizione al Cliente (Scatola!)

Quando hai finito di configurare e testare il Cucù a casa tua, devi "dimenticare" il tuo Wi-Fi di casa perché si attivi l'Hotspot a casa del cliente:

**Dalla Web UI:**
Vai in _Sistema_ e clicca la **X rossa** di fianco alla tua rete.

**Da Terminale SSH:**
```bash
sudo nmcli connection delete TUA_RETE_WIFI
sudo poweroff
```

---

## 🔍 3. Risoluzione Problemi (Troubleshooting)

Se la Web UI non risponde (`http://cucu-XXXX.local` o `http://10.42.0.1`):

**Vedere se il server Web è acceso o se è crashato:**
```bash
sudo systemctl status cucu-device-api.service
```

**Leggere gli ultimi 20 log di errore del server Web:**
```bash
sudo journalctl -u cucu-device-api.service -n 20
```

**Vedere se il controller NFC (che "legge" le statuette) sta funzionando:**
```bash
sudo systemctl status cucu-device.service
```

**Leggere in tempo reale i rilevamenti NFC (premi CTRL+C per uscire):**
```bash
sudo journalctl -u cucu-device.service -f
```

---

## 📛 4. Gestione Identità ed Emergenze Reti

Se hai usato Raspberry Pi Imager per forzare il nome (es. `cucu-setup`) e lo script non è riuscito a sovrascriverlo col nome univoco (`cucu-XXXX`), puoi forzarlo brutalmente a mano:
```bash
sudo hostnamectl set-hostname cucu-4aab
sudo sed -i 's/cucu-setup/cucu-4aab/g' /etc/hosts
sudo reboot
```

**Vedere quali reti Wi-Fi sono salvate e attive in memoria:**
```bash
nmcli connection show
```
