# cucu-device

Software di Cucù: un dispositivo per bambini che fa partire l'episodio successivo di un personaggio sulla TV quando si appoggia la sua statuina sul lettore NFC. Il genitore lo configura da una web app in rete locale.

Gira su Raspberry Pi Zero 2 W con Raspberry Pi OS Lite 64-bit, lettore NFC PN532 su I2C e LED di stato su GPIO13.

## Installazione

```bash
curl -sSL https://raw.githubusercontent.com/davidedori/cucu-device/main/bootstrap.sh | sudo bash
```

Poi `sudo reboot`. Gli aggiornamenti si avviano dalla web app (Impostazioni).
