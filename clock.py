"""Se l'ora di sistema è affidabile.

Il Pi Zero 2 W non ha RTC: senza rete, dopo un riavvio l'ora riparte da quella
salvata all'ultimo spegnimento e può essere indietro anche di giorni (spento
venerdì sera, riacceso sabato mattina dai nonni: per lui è ancora venerdì sera).
Finché l'ora non è sincronizzata i limiti di tempo non si applicano e i minuti
non si contano: meglio nessun limite che un gufetto che dorme alle 10 del
mattino. Usato da read_nfc.py e dall'API; solo stdlib.
"""
import subprocess
import time

# Finché l'ora non è affidabile si richiede a timedatectl al massimo ogni tanto
CHECK_EVERY_SEC = 60

_ok = False
_checked_at = None


def clock_ok():
    """Vero se l'ora è stata sincronizzata da quando Cucù è acceso. Una volta
    vera resta vera: senza rete il kernel smette dopo qualche ora di dirsi
    sincronizzato, ma l'orologio in corsa sbaglia di pochi secondi al giorno."""
    global _ok, _checked_at
    if _ok:
        return True
    now = time.monotonic()
    if _checked_at is not None and now - _checked_at < CHECK_EVERY_SEC:
        return False
    _checked_at = now
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except FileNotFoundError:
        # niente systemd (il computer su cui si prova l'API): ha il suo orologio
        _ok = True
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    _ok = out == "yes"
    return _ok
