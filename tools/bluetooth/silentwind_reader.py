#!/usr/bin/env python3
"""
Silentwind Hybrid-Regler (Win-Power, Bluetooth-Modul ISSC BT5050) per Bluetooth LE auslesen.

Protokoll (aus dem Mitschnitt der Original-App entschluesselt):
  Rahmen = EB 90 EB 90 | Adresse | Befehl | 00 | 20 | 32 Datenbytes | Summe (2 Byte, big endian)
  Summe  = Summe der Bytes ab "Adresse" bis Ende der Daten, 16 Bit.
  App -> Regler:  Adresse 00, Befehl 00 = Live-Werte abfragen   -> Antwort Befehl 06
                  Adresse 00, Befehl 01 = Einstellungen abfragen  -> Antwort Befehl 07
                  Adresse 00, Befehl 04 = Einstellungsblock schreiben  -> Antwort Befehl 09
  Die Bremse ist Bit 0x04 in Byte 1 des Einstellungsblocks (30 = aus, 34 = ein).
  Die App schaltet die Bremse, indem sie den GANZEN Block zurueckschreibt.
  Dieses Skript macht es genauso, aber mit Sicherungen:
    - es schreibt nur einen Block, den es unmittelbar vorher vom Regler gelesen hat
      (mit gueltiger Pruefsumme, nicht leer),
    - es aendert darin ausschliesslich das Bremsen-Bit,
    - es prueft die Echo-Antwort und danach den Bremszustand in den Live-Daten.
  Ohne --bremse sendet das Skript nur Leseanfragen.

Voraussetzung:  Python 3.10+  und  pip install bleak

Aufrufe:
  python silentwind_reader.py                       # einmal abfragen
  python silentwind_reader.py --interval 10         # alle 10 s
  python silentwind_reader.py --interval 10 --csv wind.csv
  python silentwind_reader.py --raw                 # zusaetzlich Rohdaten anzeigen
  python silentwind_reader.py --interval 60 --kurz  # nach jeder Messung trennen (App bleibt nutzbar)
  python silentwind_reader.py --bremse ein          # Bremse einschalten (mit Rueckfrage)
  python silentwind_reader.py --bremse aus --ja     # Bremse loesen ohne Rueckfrage

Bricht die Verbindung ab, verbindet das Skript nach 10 s automatisch neu.

Umrechnung: abgeglichen mit Screenshots der Original-App (Batterie 26.8 V,
Wind 10.8 V / 1.5 A / 16.7 W). Die App schneidet Nachkommastellen ab statt zu runden.
Noch unbekannt: Solar-Felder (kein Panel angeschlossen) und Byte 7.
"""

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

ADDRESS = "34:81:F4:82:77:78"
CHAR_NOTIFY = "49535343-1e4d-4bd9-ba61-23c647249616"
CHAR_WRITE = "49535343-8841-43f4-a8d4-ecbe34729bb3"

HEADER = bytes.fromhex("EB90EB90")
FRAME_LEN = 42

# Faktoren (Rohwert * Faktor), abgeglichen mit der Original-App
SCALE = {
    "batt_v": 1 / 80,     # 2144 -> 26.8 V
    "wind_v": 1 / 80,     # 864  -> 10.8 V
    "wind_a": 1 / 80,     # 124  -> 1.55 A
}


def checksum(body: bytes) -> bytes:
    return (sum(body) & 0xFFFF).to_bytes(2, "big")


def request(cmd: int, data: bytes = bytes(32)) -> bytes:
    assert len(data) == 32
    body = bytes([0x00, cmd, 0x00, 0x20]) + data
    return HEADER + body + checksum(body)


BRAKE_BIT = 0x04          # Einstellungsblock Byte 1
LIVE_BRAKE_BIT = 0x40     # Live-Daten Byte 0 (Bremse aktiv)


REQ_LIVE = request(0x00)       # EB90EB90 00 00 00 20 00..00 00 20
REQ_SETTINGS = request(0x01)   # EB90EB90 00 01 00 20 00..00 00 21
assert REQ_LIVE.hex().endswith("0020") and REQ_SETTINGS.hex().endswith("0021")


def w12(d: bytes, i: int) -> int:
    """12-Bit-Wert, linksbuendig in zwei Bytes gespeichert (z.B. 86 00 -> 0x860)"""
    return (d[i] << 4) | (d[i + 1] >> 4)


def decode_live(d: bytes) -> dict:
    batt_raw = w12(d, 2)
    wv_raw = w12(d, 4)
    wa_raw = w12(d, 14)
    wp_raw = int.from_bytes(d[8:11], "big")
    return {
        "batt_v": round(batt_raw * SCALE["batt_v"], 2),
        "wind_v": round(wv_raw * SCALE["wind_v"], 2),
        "wind_a": round(wa_raw * SCALE["wind_a"], 2),
        "wind_w": round(wp_raw / 6400, 1),   # Regler liefert Rohspannung*Rohstrom
        "batt_a": round(wp_raw / 6400 / (batt_raw * SCALE["batt_v"]), 2) if batt_raw else 0.0,
        "raw_batt": batt_raw, "raw_wind_v": wv_raw, "raw_wind_a": wa_raw,
        "raw_wind_p": wp_raw, "raw_d7": d[7], "raw_d1": d[1], "raw_d22": d[22],
        "bremse": bool(d[0] & LIVE_BRAKE_BIT),
    }


class FrameCollector:
    def __init__(self):
        self.buf = bytearray()
        self.frames = asyncio.Queue()

    def feed(self, _sender, data: bytearray):
        self.buf += data
        while True:
            i = self.buf.find(HEADER)
            if i < 0:
                del self.buf[:-3]
                return
            if i:
                del self.buf[:i]
            if len(self.buf) < FRAME_LEN:
                return
            fr = bytes(self.buf[:FRAME_LEN])
            del self.buf[:FRAME_LEN]
            if checksum(fr[4:40]) == fr[40:42]:
                self.frames.put_nowait(fr)

    async def wait_for(self, cmd: int, timeout=3.0) -> bytes:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                raise TimeoutError("keine Antwort")
            fr = await asyncio.wait_for(self.frames.get(), left)
            if fr[5] == cmd:
                return fr


CSV_FIELDS = ["zeit", "bremse", "batt_v", "batt_a", "wind_v", "wind_a", "wind_w", "raw_batt", "raw_wind_v",
              "raw_wind_a", "raw_wind_p", "raw_d7", "daten_hex"]


def write_csv(path, now, v, d):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if new:
            w.writerow(CSV_FIELDS)
        w.writerow([now.isoformat(timespec="seconds")] + [v[k] for k in CSV_FIELDS[1:-1]] + [d.hex(" ")])


async def poll_once(client, col, a):
    await client.write_gatt_char(CHAR_WRITE, REQ_LIVE, response=True)
    fr = await col.wait_for(0x06)
    d = fr[8:40]
    v = decode_live(d)
    now = datetime.now()
    print(f"{now:%H:%M:%S}  Wind {v['wind_v']:5.2f} V {v['wind_a']:5.2f} A {v['wind_w']:6.1f} W   "
          f"Batterie {v['batt_v']:5.2f} V  ~{v['batt_a']:4.2f} A (berechnet)"
          + ("   BREMSE AN" if v["bremse"] else ""))
    if a.raw:
        print(f"          roh: batt={v['raw_batt']} windV={v['raw_wind_v']} windA={v['raw_wind_a']} "
              f"windP={v['raw_wind_p']} d7={v['raw_d7']}   {d.hex(' ')}")
    if a.csv:
        try:
            write_csv(a.csv, now, v, d)
        except PermissionError:
            print(f"          CSV gesperrt ({a.csv} in Excel offen?) - Zeile nicht geschrieben")


async def session(a) -> None:
    """Eine Verbindung: verbinden, abfragen bis Fehler/Ende. Wirft Exception bei Verbindungsverlust."""
    dev = await BleakScanner.find_device_by_address(a.address, timeout=15.0)
    if dev is None:
        raise ConnectionError("Regler nicht gefunden (Reichweite? von einem Handy belegt?)")
    col = FrameCollector()
    lost = asyncio.Event()
    async with BleakClient(dev, timeout=20.0, disconnected_callback=lambda _c: lost.set()) as client:
        await client.start_notify(CHAR_NOTIFY, col.feed)
        if not a.settings_done:
            a.settings_done = True
            await client.write_gatt_char(CHAR_WRITE, REQ_SETTINGS, response=True)
            try:
                fr = await col.wait_for(0x07)
                print("Einstellungen (roh):", fr[8:40].hex(" "))
            except TimeoutError:
                print("Einstellungen: keine Antwort")
        misses = 0
        while not lost.is_set():
            t0 = asyncio.get_running_loop().time()
            try:
                await poll_once(client, col, a)
                misses = 0
            except TimeoutError:
                misses += 1
                print(f"{datetime.now():%H:%M:%S}  keine Antwort")
                if misses >= 3:
                    raise ConnectionError("3x keine Antwort")
            if a.interval <= 0:
                return
            if a.kurz:
                return          # Verbindung freigeben, naechste Messung verbindet neu
            rest = a.interval - (asyncio.get_running_loop().time() - t0)
            try:
                await asyncio.wait_for(lost.wait(), max(0.5, rest))
            except asyncio.TimeoutError:
                pass
        raise ConnectionError("Verbindung verloren")


async def set_brake(a, on: bool) -> bool:
    """Bremse schalten. Liefert True, wenn der Regler den neuen Zustand bestaetigt."""
    dev = await BleakScanner.find_device_by_address(a.address, timeout=15.0)
    if dev is None:
        raise ConnectionError("Regler nicht gefunden (Reichweite? von einem Handy belegt?)")
    col = FrameCollector()
    async with BleakClient(dev, timeout=20.0) as client:
        await client.start_notify(CHAR_NOTIFY, col.feed)
        # 1. aktuellen Einstellungsblock lesen
        await client.write_gatt_char(CHAR_WRITE, REQ_SETTINGS, response=True)
        cur = (await col.wait_for(0x07))[8:40]
        if cur == bytes(32) or cur[0] == 0:
            raise RuntimeError("Einstellungsblock sieht ungueltig aus - es wird NICHTS geschrieben")
        is_on = bool(cur[1] & BRAKE_BIT)
        if is_on == on:
            print(f"Bremse ist bereits {'EIN' if on else 'AUS'} - nichts zu tun.")
            return None
        # 2. nur das Bremsen-Bit aendern
        new = bytearray(cur)
        new[1] = (cur[1] | BRAKE_BIT) if on else (cur[1] & ~BRAKE_BIT & 0xFF)
        diff = [i for i in range(32) if new[i] != cur[i]]
        assert diff == [1], diff
        await client.write_gatt_char(CHAR_WRITE, request(0x04, bytes(new)), response=True)
        echo = (await col.wait_for(0x09))[8:40]
        if echo != bytes(new):
            raise RuntimeError("Regler hat einen anderen Block bestaetigt: " + echo.hex(" "))
        # 3. Zustand in den Live-Daten pruefen
        for _ in range(5):
            await client.write_gatt_char(CHAR_WRITE, REQ_LIVE, response=True)
            try:
                d = (await col.wait_for(0x06))[8:40]
                if bool(d[0] & LIVE_BRAKE_BIT) == on:
                    return True
            except TimeoutError:
                pass
            await asyncio.sleep(1)
        return False


async def brake_cmd(a):
    on = a.bremse == "ein"
    if not a.ja:
        txt = "EINSCHALTEN" if on else "LOESEN"
        if input(f"Bremse {txt}? Mit 'j' bestaetigen: ").strip().lower() not in ("j", "ja", "y"):
            print("Abgebrochen.")
            return
    for versuch in range(1, 4):
        try:
            ok = await set_brake(a, on)
            if ok is None:
                return
            if ok:
                print(f"{datetime.now():%H:%M:%S}  Bremse {'EIN' if on else 'AUS'} - vom Regler bestaetigt.")
            else:
                print("Befehl angenommen, aber der Live-Zustand hat noch nicht umgeschaltet. Bitte pruefen!")
            return
        except Exception as e:  # noqa: BLE001
            print(f"Versuch {versuch}/3 fehlgeschlagen: {e or type(e).__name__}")
            await asyncio.sleep(2)
    print("BREMSE NICHT GESCHALTET - bitte von Hand schalten!")


async def main():
    ap = argparse.ArgumentParser(description="Silentwind-Regler per BLE auslesen")
    ap.add_argument("--address", default=ADDRESS)
    ap.add_argument("--interval", type=int, default=0, help="Sekunden zwischen Messungen")
    ap.add_argument("--csv")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--kurz", action="store_true",
                    help="nach jeder Messung trennen (Regler bleibt fuer die App frei)")
    ap.add_argument("--bremse", choices=["ein", "aus"], help="Bremse schalten")
    ap.add_argument("--ja", action="store_true", help="ohne Rueckfrage (fuer Verknuepfungen)")
    a = ap.parse_args()

    if a.bremse:
        await brake_cmd(a)
        return

    a.settings_done = False
    while True:
        t0 = asyncio.get_running_loop().time()
        try:
            await session(a)
            if a.interval <= 0:
                return
            if a.kurz:
                await asyncio.sleep(max(1.0, a.interval - (asyncio.get_running_loop().time() - t0)))
        except Exception as e:  # noqa: BLE001  (BleakError, ConnectionError, TimeoutError ...)
            if a.interval <= 0:
                print(f"Fehler: {e}")
                return
            print(f"{datetime.now():%H:%M:%S}  Verbindung weg ({e or type(e).__name__}) - neuer Versuch in 10 s")
            await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
