#!/usr/bin/env python3
"""Lettore NFC di read_nfc.py: modulo PN532 sul bus I2C dei GPIO.

read_uid() -> str | None, con l'UID in hex minuscolo separato da spazi
("04 a1 b2 c3 d4 e5 f6"): è il formato delle chiavi di tags.json, lo stesso
che usava il vecchio lettore USB ACR122U (supporto rimosso), quindi le
statuette associate allora restano valide.

Solo stdlib: read_nfc.py gira col python di sistema, non nel venv dell'API.

Eseguito direttamente (`python3 nfc_reader.py`) stampa gli UID letti: utile
per verificare il cablaggio col servizio fermo.
"""
import fcntl
import os
import time

I2C_BUS_PATH = "/dev/i2c-1"
PN532_I2C_ADDR = 0x24
I2C_SLAVE = 0x0703  # ioctl da <linux/i2c-dev.h>

# Comandi PN532 (User Manual NXP UM0701-02)
CMD_GET_FIRMWARE_VERSION = 0x02
CMD_SAM_CONFIGURATION = 0x14
CMD_RF_CONFIGURATION = 0x32
CMD_IN_LIST_PASSIVE_TARGET = 0x4A

ACK_FRAME = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])
HOST_TO_PN532 = 0xD4
PN532_TO_HOST = 0xD5

# Quanti poll falliti di fila (errori I2C, non "nessun tag") assorbire
# restituendo l'ultimo UID valido: un glitch del bus non deve sembrare una
# statuetta tolta, altrimenti il video va in pausa da solo. ~3 tick = ~300ms.
ERROR_HOLD_POLLS = 3
# Dopo quanti errori di fila chiudere/riaprire il bus e reinizializzare il chip.
REINIT_AFTER_ERRORS = 10
# Una statuetta al limite della portata (es. dietro lo spessore del case)
# può mancare un poll isolato, soprattutto mentre viene appoggiata. Il tag
# viene dato per tolto solo dopo questo tempo di assenza continuativa,
# altrimenti il video alterna pausa/ripresa ("a scatti").
ABSENCE_CONFIRM_SEC = 0.5


class Pn532Error(Exception):
    pass


def format_uid(uid_bytes):
    return " ".join(f"{b:02x}" for b in uid_bytes)


# --- PN532 frame (funzioni pure, testabili senza hardware) --------------

def build_frame(cmd, params=b""):
    """Frame informativo normale: 00 00 FF LEN LCS D4 CMD params DCS 00."""
    data = bytes([HOST_TO_PN532, cmd]) + bytes(params)
    length = len(data)
    lcs = (-length) & 0xFF
    dcs = (-sum(data)) & 0xFF
    return bytes([0x00, 0x00, 0xFF, length, lcs]) + data + bytes([dcs, 0x00])


def parse_response(buf, cmd):
    """Estrae i dati di risposta a `cmd` da un buffer letto dal PN532 (senza
    il byte di status I2C). Solleva Pn532Error se il frame non è valido."""
    start = buf.find(b"\x00\xff")
    if start < 0 or start + 4 > len(buf):
        raise Pn532Error("preambolo non trovato")
    i = start + 2
    length, lcs = buf[i], buf[i + 1]
    if (length + lcs) & 0xFF != 0:
        raise Pn532Error("checksum lunghezza errato")
    body = buf[i + 2:i + 2 + length]
    if len(body) != length or i + 2 + length >= len(buf):
        raise Pn532Error("frame troncato")
    dcs = buf[i + 2 + length]
    if (sum(body) + dcs) & 0xFF != 0:
        raise Pn532Error("checksum dati errato")
    if length == 1 and body[0] == 0x7F:
        raise Pn532Error("errore applicativo PN532")
    if length < 2 or body[0] != PN532_TO_HOST or body[1] != cmd + 1:
        raise Pn532Error(f"risposta inattesa: {body.hex()}")
    return bytes(body[2:])


def parse_passive_target_uid(data):
    """Risposta di InListPassiveTarget (106 kbps tipo A):
    NbTg, Tg, SENS_RES(2), SEL_RES, NFCIDLength, NFCID1..."""
    if not data or data[0] == 0:
        return None
    if len(data) < 6:
        raise Pn532Error("risposta InListPassiveTarget troncata")
    uid_len = data[5]
    uid = data[6:6 + uid_len]
    if len(uid) != uid_len or uid_len == 0:
        raise Pn532Error("UID troncato")
    return format_uid(uid)


# --- PN532 su I2C -------------------------------------------------------

class Pn532I2cReader:
    name = "PN532 I2C"

    def __init__(self, bus_path=I2C_BUS_PATH, addr=PN532_I2C_ADDR):
        self.bus_path = bus_path
        self.addr = addr
        self.fd = None
        self.firmware = None
        self._last_uid = None
        self._errors = 0
        self._absent_since = None

    # -- trasporto --

    def _open(self):
        self.close()
        self.fd = os.open(self.bus_path, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.addr)

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def _wait_ready(self, timeout):
        # Su I2C ogni lettura inizia con un byte di status: bit0 = risposta pronta.
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.read(self.fd, 1)[0] & 0x01:
                    return
            except OSError:
                # NACK durante il clock stretching: il chip è occupato, riprova
                pass
            if time.monotonic() > deadline:
                raise Pn532Error("timeout in attesa del PN532")
            time.sleep(0.005)

    def _read(self, n):
        # +1 per il byte di status, che scartiamo
        return os.read(self.fd, n + 1)[1:]

    def _command(self, cmd, params=b"", timeout=0.5, response_len=48):
        os.write(self.fd, build_frame(cmd, params))
        self._wait_ready(timeout)
        if self._read(len(ACK_FRAME)) != ACK_FRAME:
            raise Pn532Error("ACK non ricevuto")
        self._wait_ready(timeout)
        return parse_response(self._read(response_len), cmd)

    # -- inizializzazione --

    def _abort_pending(self):
        # Un riavvio del servizio può uccidere il processo precedente a metà di
        # un InListPassiveTarget: il chip resta con quel comando in corso (o
        # con la risposta non letta) e il primo scambio del nuovo processo va
        # fuori sincrono. Un frame ACK dall'host annulla il comando in corso
        # (UM0701-02, par. 6.2.1.3); se non c'era niente da annullare è ignorato.
        try:
            os.write(self.fd, ACK_FRAME)
        except OSError:
            pass
        time.sleep(0.02)

    def _init_chip(self):
        self._abort_pending()
        # Il PN532 si sveglia dal power-down al primo indirizzamento I2C, e il
        # primo trasferimento può andare in NACK: qualche tentativo prima di
        # considerarlo assente.
        last_error = None
        for _ in range(3):
            try:
                fw = self._command(CMD_GET_FIRMWARE_VERSION, timeout=0.2)
                break
            except (OSError, Pn532Error) as e:
                last_error = e
                time.sleep(0.05)
        else:
            raise Pn532Error(f"PN532 non risponde su {self.bus_path}@0x{self.addr:02x}: {last_error}")
        if len(fw) >= 3:
            self.firmware = f"{fw[1]}.{fw[2]}"
        # Normal mode, timeout 0x14 (1s), IRQ usato
        self._command(CMD_SAM_CONFIGURATION, bytes([0x01, 0x14, 0x01]))
        # MaxRetries: MxRtyATR=0xFF, MxRtyPSL=0x01, MxRtyPassiveActivation=0x10.
        # Il default (0xFF) fa aspettare InListPassiveTarget all'infinito
        # finché non arriva un tag. 0x10 (17 tentativi) è il compromesso
        # misurato con la statuetta dietro il case: 60/60 letture riuscite,
        # ~40ms per poll con tag e ~110ms senza tag (con 0x02 ~90% di letture).
        self._command(CMD_RF_CONFIGURATION, bytes([0x05, 0xFF, 0x01, 0x10]))

    def probe(self):
        """Apre il bus e inizializza il chip. Ritorna True se il PN532 risponde."""
        if not os.path.exists(self.bus_path):
            return False
        try:
            self._open()
            self._init_chip()
            return True
        except (OSError, Pn532Error):
            self.close()
            return False

    # -- polling --

    def _poll(self):
        data = self._command(CMD_IN_LIST_PASSIVE_TARGET, bytes([0x01, 0x00]))
        # Niente InRelease dopo la lettura: mette il tag in HALT e, al limite
        # della portata, il poll successivo spesso non riesce a riattivarlo
        # (misurato: letture alternate 0101..., ~40% di successo). Senza
        # rilascio la rimozione viene comunque rilevata al poll successivo.
        return parse_passive_target_uid(data)

    def read_uid(self):
        try:
            if self.fd is None:
                self._open()
                self._init_chip()
            uid = self._poll()
        except (OSError, Pn532Error) as e:
            self._errors += 1
            if self._errors == 1 or self._errors % REINIT_AFTER_ERRORS == 0:
                print(f"[WARN] Errore PN532 ({self._errors} di fila): {e}")
            if self._errors % REINIT_AFTER_ERRORS == 0:
                # Riaprirà bus e chip al prossimo poll
                self.close()
            if self._errors <= ERROR_HOLD_POLLS:
                return self._last_uid
            self._last_uid = None
            return None
        if self._errors:
            print(f"[INFO] PN532 di nuovo operativo dopo {self._errors} errori.")
            self._errors = 0
        if uid is None and self._last_uid is not None:
            now = time.monotonic()
            if self._absent_since is None:
                self._absent_since = now
            if now - self._absent_since < ABSENCE_CONFIRM_SEC:
                return self._last_uid
        self._absent_since = None
        self._last_uid = uid
        return uid


# --- AVVIO ---------------------------------------------------------------

# Ogni quanto ripetere l'avviso se il PN532 continua a non rispondere
NOT_FOUND_WARN_EVERY_SEC = 60


def create_reader():
    """Ritorna il lettore solo quando il PN532 risponde. Finché non risponde
    riprova invece di far crashare il servizio: resta "active" e mostra la
    schermata idle, senza cicli di restart di systemd."""
    reader = Pn532I2cReader()
    tries = 0
    last_warn = None
    while not reader.probe():
        tries += 1
        now = time.monotonic()
        if last_warn is None or now - last_warn >= NOT_FOUND_WARN_EVERY_SEC:
            print(f"[WARN] PN532 non risponde su {I2C_BUS_PATH}@0x{PN532_I2C_ADDR:02x}, riprovo... "
                  f"(tentativo {tries})")
            last_warn = now
        time.sleep(2)
    detail = f" (fw {reader.firmware})" if reader.firmware else ""
    after = f" dopo {tries + 1} tentativi" if tries else ""
    print(f"Lettore NFC: {reader.name}{detail}{after}")
    return reader


if __name__ == "__main__":
    r = create_reader()
    last = object()
    try:
        while True:
            uid = r.read_uid()
            if uid != last:
                print(uid if uid else "(nessun tag)")
                last = uid
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
