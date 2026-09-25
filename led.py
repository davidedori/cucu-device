#!/usr/bin/env python3
"""LED di stato di Cucù (hardware v2), servizio cucu-led.service.

Gira separato da read_nfc.py: parte presto nel boot (prima di rete e VLC) e
anima il LED a 50Hz con il PWM hardware, indipendentemente dal loop NFC.

Comportamento:
- avvio, o read_nfc.py fermo/bloccato → respiro veloce
- idle, pausa, fine episodio          → respiro lento
- video in corso, o visione bloccata  → fisso basso
  dai limiti di tempo

Lo stato viene letto da last_seen_tag.json, che read_nfc.py riscrive ad ogni
tick con "mode", "blocked" e "ts". Se il file non è aggiornato da
STALE_AFTER_SEC, il programma principale non sta girando → respiro veloce.

Collegamento: LED su GPIO13 (pin 33), GND sul pin 25. Richiede in config.txt
`dtoverlay=pwm,pin=13,func=4` (lo aggiunge setup.sh). Sui dispositivi senza
PWM su GPIO13 il servizio esce senza errori e non tocca nient'altro.
"""
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "last_seen_tag.json"

PWM_CHIP = Path("/sys/class/pwm/pwmchip0")
PWM_CHANNEL = 1          # GPIO13 = PWM1
PWM_PERIOD_NS = 100_000  # 10 kHz: nessuno sfarfallio visibile

# --- ASPETTO (regolare qui dopo le modifiche al case) -------------------
# Valori in % di duty cycle. Il LED da 5V pilotato a 3,3V è già attenuato
# dalla sua resistenza integrata: questi valori sono stati scelti a occhio
# sul prototipo v2.
BREATH_MAX = 20.0        # picco del respiro
BREATH_MIN = 8.0         # minimo del respiro (mai spento)
STEADY = 8.0             # fisso durante la riproduzione / visione bloccata
SLOW_PERIOD_SEC = 5.0    # respiro lento: idle, pausa
FAST_PERIOD_SEC = 1.5    # respiro veloce: avvio, programma principale fermo
FADE_SEC = 0.6           # dissolvenza tra un effetto e l'altro

FRAME_SEC = 0.02         # 50 Hz
STATE_POLL_SEC = 0.2
STALE_AFTER_SEC = 10.0   # oltre questo read_nfc.py è considerato fermo
PWM_WAIT_SEC = 30.0      # attesa massima del driver PWM al boot

GAMMA = 2.2


def breath(t, period):
    """Respiro con luminosità PERCEPITA sinusoidale: si interpola in spazio
    percettivo (duty^(1/gamma)) e si riconverte, così minimo e picco durano
    uguale all'occhio."""
    x = (1 - math.cos(2 * math.pi * (t % period) / period)) / 2
    lo, hi = BREATH_MIN ** (1 / GAMMA), BREATH_MAX ** (1 / GAMMA)
    return (lo + (hi - lo) * x) ** GAMMA


EFFECTS = {
    "fast": lambda t: breath(t, FAST_PERIOD_SEC),
    "slow": lambda t: breath(t, SLOW_PERIOD_SEC),
    "steady": lambda t: STEADY,
}


def effect_for_state(state, now):
    if state is None or now - state.get("ts", 0) > STALE_AFTER_SEC:
        return "fast"
    if state.get("mode") == "playing" or state.get("blocked"):
        return "steady"
    return "slow"


def read_state():
    """Ritorna il contenuto di last_seen_tag.json, None se manca, o solleva
    ValueError se è illeggibile (read_nfc.py lo sta riscrivendo proprio ora:
    il chiamante tiene lo stato precedente)."""
    try:
        with STATE_FILE.open() as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(e)


class HardwarePwm:
    def __init__(self):
        self.path = PWM_CHIP / f"pwm{PWM_CHANNEL}"
        self._duty_fd = None
        self._last_duty = None

    def _write(self, name, value):
        with open(self.path / name, "w") as f:
            f.write(str(value))

    def open(self):
        deadline = time.monotonic() + PWM_WAIT_SEC
        # Partendo presto nel boot, il chip PWM può esistere ma essere ancora
        # di root: udev lo assegna al gruppo gpio poco dopo (visto dal vivo:
        # Permission denied su export ~2s dopo l'avvio del servizio).
        while not os.access(PWM_CHIP / "export", os.W_OK):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.1)
        if not self.path.exists():
            with open(PWM_CHIP / "export", "w") as f:
                f.write(str(PWM_CHANNEL))
        # Dopo l'export udev assegna il gruppo gpio ai file del canale: fino
        # ad allora non sono scrivibili dall'utente del servizio.
        while not os.access(self.path / "period", os.W_OK):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        self._write("duty_cycle", 0)
        self._write("period", PWM_PERIOD_NS)
        self._write("enable", 1)
        # Il file duty_cycle resta aperto: riaprirlo 50 volte al secondo
        # costava ~5% di un core, non trascurabile con VLC che decodifica.
        self._duty_fd = os.open(self.path / "duty_cycle", os.O_WRONLY)
        return True

    def set(self, percent):
        percent = max(0.0, min(100.0, percent))
        duty = int(PWM_PERIOD_NS * percent / 100)
        if duty == self._last_duty:
            return  # es. luce fissa durante il video: nessuna scrittura
        os.pwrite(self._duty_fd, str(duty).encode(), 0)
        self._last_duty = duty

    def off(self):
        try:
            self._write("duty_cycle", 0)
            self._write("enable", 0)
        except OSError:
            pass


def main():
    pwm = HardwarePwm()
    try:
        if not pwm.open():
            print("PWM su GPIO13 non disponibile: LED di stato disattivato.")
            return 0
    except OSError as e:
        print(f"PWM su GPIO13 non utilizzabile ({e}): LED di stato disattivato.")
        return 0

    def stop(*_):
        pwm.off()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    print("LED di stato attivo.")
    t0 = time.monotonic()
    state = None
    effect = "fast"
    fade_from, fade_start = None, 0.0
    output = 0.0
    next_poll = 0.0

    while True:
        mono = time.monotonic()
        t = mono - t0

        if mono >= next_poll:
            next_poll = mono + STATE_POLL_SEC
            try:
                state = read_state()
            except ValueError:
                pass
            wanted = effect_for_state(state, time.time())
            if wanted != effect:
                print(f"LED: {effect} → {wanted}")
                effect = wanted
                fade_from, fade_start = output, mono

        target = EFFECTS[effect](t)
        if fade_from is not None:
            a = (mono - fade_start) / FADE_SEC
            if a >= 1:
                fade_from = None
            else:
                a = a * a * (3 - 2 * a)  # smoothstep
                target = fade_from + (target - fade_from) * a
        output = target

        try:
            pwm.set(output)
        except OSError as e:
            print(f"[WARN] Scrittura PWM fallita: {e}")
        time.sleep(FRAME_SEC)


if __name__ == "__main__":
    sys.exit(main())
