#!/usr/bin/env python3
"""
Daly Smart BMS (neues Modbus-Protokoll, 0xD2) per Bluetooth auslesen.

Arbeitsweise: verbinden -> Daten lesen -> sofort trennen.
So ist das BMS zwischen den Abfragen frei fuer die Daly-App auf den Handys.

Voraussetzung:  Python 3.10+  und  pip install bleak

Aufrufe:
  python daly_ble_reader.py --scan              # BLE-Geraete in Reichweite auflisten
  python daly_ble_reader.py                     # alle BMS aus BMS_LIST einmal abfragen
  python daly_ble_reader.py --interval 60       # alle 60 s wiederholen
  python daly_ble_reader.py --interval 60 --csv log.csv   # zusaetzlich in CSV mitschreiben (Excel-tauglich)
  python daly_ble_reader.py --cells             # Einzelzellspannungen mit ausgeben
"""

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

# --------------------------------------------------------------------------
# HIER ANPASSEN: Name (frei waehlbar) und Bluetooth-Name ODER MAC-Adresse.
# Den Bluetooth-Namen bzw. die MAC zeigt "--scan" oder nRF Connect an.
# --------------------------------------------------------------------------
# MAC-Adressen sind zuverlaessiger und schneller als Namen (Namen: Gross-/Kleinschreibung egal).
BMS_LIST = [
    {"name": "Batterie 1", "ble": "40:18:03:01:51:BC"},   # "Isme Bat 1 BMS"
    {"name": "Batterie 2", "ble": "40:18:03:01:50:DE"},   # "Isme Bat 2 BMS"
    {"name": "Batterie 3", "ble": "40:18:03:01:4B:62"},   # "Isme Bat 3 BMS"
]

CHAR_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"

# Modbus: Adresse 0xD2, Funktion 03, ab Register 0, 62 Register (0x3E)
REQUEST_STATUS = bytes.fromhex("D2030000003ED7B9")

CONNECT_TIMEOUT = 15.0
RESPONSE_TIMEOUT = 5.0
RETRIES = 2


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def decode_status(frame: bytes) -> dict:
    """Antwort auf REQUEST_STATUS dekodieren. Wirft ValueError bei Fehlern."""
    if len(frame) < 5 or frame[0] != 0xD2 or frame[1] != 0x03:
        raise ValueError(f"unerwarteter Frame-Anfang: {frame[:3].hex(' ')}")
    n = frame[2]
    if len(frame) < 3 + n + 2:
        raise ValueError(f"Frame unvollstaendig ({len(frame)} von {3 + n + 2} Bytes)")
    frame = frame[: 3 + n + 2]
    crc_rx = frame[-2] | (frame[-1] << 8)
    if modbus_crc(frame[:-2]) != crc_rx:
        raise ValueError("Pruefsumme falsch")

    data = frame[3:-2]
    r = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]
    if len(r) < 58:
        raise ValueError(f"zu wenige Register ({len(r)})")

    cell_count = r[49]
    temp_count = r[50]
    status_txt = {0: "Ruhe", 1: "Laden", 2: "Entladen"}.get(r[47], f"? ({r[47]})")

    return {
        "spannung_v": r[40] / 10,
        "strom_a": round((r[41] - 30000) / 10, 1),
        "soc_pct": r[42] / 10,
        "rest_ah": r[48] / 10,
        "leistung_w": r[57],                      # unbestaetigt
        "status": status_txt,                     # Zuordnung unbestaetigt
        "zelle_max_mv": r[43],
        "zelle_min_mv": r[44],
        "zelle_diff_mv": r[56],                   # unbestaetigt
        "zelle_mittel_mv": r[55],
        "temp_max_c": r[45] - 40,
        "temp_min_c": r[46] - 40,
        "zyklen": r[51],
        "lade_mosfet": bool(r[53]),
        "entlade_mosfet": bool(r[54]),
        "zellen_mv": r[0:min(cell_count, 32)],
        "temps_c": [t - 40 for t in r[32:32 + min(temp_count, 8)]],
        "alarm_roh": " ".join(f"{x:04X}" for x in r[58:62]),  # Bedeutung noch unbekannt
    }


async def resolve_device(ble: str):
    """Geraet per MAC oder Bluetooth-Namen finden."""
    if ":" in ble or ("-" in ble and len(ble) > 30):  # MAC (Windows) oder UUID (macOS)
        return await BleakScanner.find_device_by_address(ble, timeout=10.0)
    wanted = ble.strip().lower()
    return await BleakScanner.find_device_by_filter(
        lambda dev, adv: (dev.name or adv.local_name or "").strip().lower() == wanted,
        timeout=10.0,
    )


async def read_bms(ble: str) -> dict:
    device = await resolve_device(ble)
    if device is None:
        raise RuntimeError("nicht gefunden (ausser Reichweite oder von einem Handy belegt?)")

    buf = bytearray()
    done = asyncio.Event()

    def on_notify(_sender, data: bytearray):
        buf.extend(data)
        # Anfang suchen und pruefen, ob der Frame komplett ist
        start = buf.find(b"\xD2\x03")
        if start > 0:
            del buf[:start]
        if len(buf) >= 3 and len(buf) >= 3 + buf[2] + 2:
            done.set()

    async with BleakClient(device, timeout=CONNECT_TIMEOUT) as client:
        await client.start_notify(CHAR_NOTIFY, on_notify)
        await client.write_gatt_char(CHAR_WRITE, REQUEST_STATUS, response=False)
        try:
            await asyncio.wait_for(done.wait(), RESPONSE_TIMEOUT)
        finally:
            try:
                await client.stop_notify(CHAR_NOTIFY)
            except Exception:
                pass
    # Hier ist die Verbindung bereits getrennt (Ende von "async with").
    return decode_status(bytes(buf))


async def read_with_retry(ble: str) -> dict:
    last = None
    for attempt in range(1 + RETRIES):
        try:
            return await read_bms(ble)
        except Exception as e:  # noqa: BLE001
            last = e
            await asyncio.sleep(2)
    raise RuntimeError(str(last) or type(last).__name__)


def print_result(name: str, d: dict, show_cells: bool):
    print(f"\n== {name} ==")
    print(f"  {d['spannung_v']:5.1f} V   {d['strom_a']:+6.1f} A   {d['leistung_w']:4d} W   "
          f"SOC {d['soc_pct']:5.1f} %   Rest {d['rest_ah']:.1f} Ah   ({d['status']})")
    print(f"  Zellen: min {d['zelle_min_mv']} mV  max {d['zelle_max_mv']} mV  "
          f"Diff {d['zelle_diff_mv']} mV   Temp {d['temp_min_c']}..{d['temp_max_c']} °C   "
          f"Zyklen {d['zyklen']}")
    print(f"  MOSFET Laden: {'an' if d['lade_mosfet'] else 'AUS'}   "
          f"Entladen: {'an' if d['entlade_mosfet'] else 'AUS'}   Alarm-Rohwerte: {d['alarm_roh']}")
    if show_cells:
        cells = "  ".join(f"{i + 1}:{v / 1000:.3f}" for i, v in enumerate(d["zellen_mv"]))
        print(f"  {cells}")


# CSV im "langen" Format: eine Zeile pro Batterie und Messung.
# Passt zur Excel-Vorlage BMS_Auswertung.xlsx (dort ins Blatt "Daten" einfuegen).
# Zeit im ISO-Format, Dezimalpunkt (Schweiz), Trennzeichen ";".
CSV_FIELDS = ["zeit", "batterie", "spannung_v", "strom_a", "leistung_w", "soc_pct", "rest_ah",
              "status", "zelle_min_mv", "zelle_max_mv", "zelle_diff_mv", "temp_min_c",
              "temp_max_c", "zyklen", "lade_mosfet", "entlade_mosfet", "alarm_roh"] + \
             [f"zelle{i}_mv" for i in range(1, 9)]


def append_csv(path: str, ts: datetime, results: dict):
    """results: {bms_name: dict oder None (Fehler -> keine Zeile)}"""
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=";", extrasaction="ignore")
        if new:
            w.writeheader()
        for name, d in results.items():
            if not d:
                continue
            row = {k: d.get(k) for k in CSV_FIELDS}
            row["zeit"] = datetime.now().isoformat(timespec="seconds")
            row["batterie"] = name
            for i, v in enumerate(d["zellen_mv"][:8], start=1):
                row[f"zelle{i}_mv"] = v
            w.writerow(row)


async def scan():
    print("Suche 10 s nach Bluetooth-Geraeten ...")
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    for dev, adv in sorted(found.values(), key=lambda x: -x[1].rssi):
        print(f"  {dev.address}   {adv.rssi:4d} dBm   {dev.name or adv.local_name or '-'}")


async def main():
    ap = argparse.ArgumentParser(description="Daly BMS per Bluetooth auslesen")
    ap.add_argument("--scan", action="store_true", help="BLE-Geraete auflisten")
    ap.add_argument("--interval", type=int, default=0, help="Wiederholen alle N Sekunden")
    ap.add_argument("--csv", help="Ergebnisse an diese CSV-Datei anhaengen")
    ap.add_argument("--cells", action="store_true", help="Einzelzellspannungen anzeigen")
    args = ap.parse_args()

    if args.scan:
        await scan()
        return

    loop = asyncio.get_running_loop()
    while True:
        t0 = loop.time()
        ts = datetime.now()
        print(f"\n--- {ts:%Y-%m-%d %H:%M:%S} ---")
        results = {}
        for bms in BMS_LIST:
            try:
                d = await read_with_retry(bms["ble"])
                print_result(bms["name"], d, args.cells)
                results[bms["name"]] = d
            except Exception as e:  # noqa: BLE001
                print(f"\n== {bms['name']} ==\n  FEHLER: {e}")
                results[bms["name"]] = None
        if args.csv:
            try:
                append_csv(args.csv, ts, results)
            except PermissionError:
                print(f"\n  CSV nicht geschrieben: {args.csv} ist gesperrt (in Excel geoeffnet?). "
                      "Fuer Excel eine Kopie oeffnen.")
        if args.interval <= 0:
            break
        # festen Takt halten (Abfragedauer wird abgezogen)
        await asyncio.sleep(max(1.0, args.interval - (loop.time() - t0)))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
