#!/usr/bin/env python3
"""
Silentwind / Win-Power Laderegler per Bluetooth erkunden.

Das Protokoll ist nicht dokumentiert. Dieses Skript sammelt deshalb erst einmal
alles, was das Geraet von sich gibt, und schreibt es in eine Logdatei.

Voraussetzung:  Python 3.10+  und  pip install bleak

Aufrufe:
  python silentwind_ble_explore.py --scan            # Geraete in Reichweite auflisten
  python silentwind_ble_explore.py                   # verbinden, alles auflisten, 30 s zuhoeren
  python silentwind_ble_explore.py --listen 60       # 60 s zuhoeren
  python silentwind_ble_explore.py --probe           # zusaetzlich Lese-Anfragen senden (mit Rueckfrage)

WICHTIG: Die Silentwind-App auf allen Handys schliessen, sonst ist der Regler belegt.
Ohne --probe sendet das Skript KEINE Bytes an den Regler: es liest nur die
Bluetooth-Kanaele und hoert zu. Einstellungen koennen dabei nicht veraendert werden.
"""

import argparse
import asyncio
import sys
from datetime import datetime

from bleak import BleakClient, BleakScanner

DEFAULT_ADDRESS = "34:81:F4:82:77:78"   # WIN-POWER (aus nRF Connect)

# Microchip "Transparent UART" (BM70 / RN4870 / RN4871) - wird von vielen
# chinesischen Reglern als Bluetooth-zu-Seriell-Bruecke verwendet.
MCHP_SERVICE = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
MCHP_TX_NOTIFY = "49535343-1e4d-4bd9-ba61-23c647249616"   # Geraet -> Handy
MCHP_RX_WRITE = "49535343-8841-43f4-a8d4-ecbe34729bb3"    # Handy -> Geraet

LOGFILE = f"silentwind_log_{datetime.now():%Y%m%d_%H%M%S}.txt"


def log(msg: str):
    line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + msg
    print(line)
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def hexdump(b: bytes) -> str:
    asc = "".join(chr(x) if 32 <= x < 127 else "." for x in b)
    return f"{b.hex(' ').upper()}   |{asc}|"


def modbus_crc(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes([crc & 0xFF, crc >> 8])


async def scan():
    print("Suche 10 s nach Bluetooth-Geraeten ...")
    found = await BleakScanner.discover(timeout=10.0, return_adv=True)
    for dev, adv in sorted(found.values(), key=lambda x: -x[1].rssi):
        name = dev.name or adv.local_name or "-"
        mark = "   <==" if "win" in name.lower() or "silent" in name.lower() else ""
        print(f"  {dev.address}   {adv.rssi:4d} dBm   {name}{mark}")


async def explore(address: str, listen_s: int, probe: bool):
    log(f"Logdatei: {LOGFILE}")
    log(f"Verbinde mit {address} ...")
    device = await BleakScanner.find_device_by_address(address, timeout=15.0)
    if device is None:
        log("Geraet nicht gefunden (ausser Reichweite oder von einem Handy belegt?)")
        return

    async with BleakClient(device, timeout=20.0) as client:
        log(f"Verbunden. MTU = {client.mtu_size}")

        notify_chars, write_chars = [], []
        log("=== Dienste und Charakteristiken ===")
        for svc in client.services:
            log(f"Dienst {svc.uuid}  ({svc.description})")
            for ch in svc.characteristics:
                props = ",".join(ch.properties)
                log(f"   Char {ch.uuid}  [{props}]  handle={ch.handle}  ({ch.description})")
                if "read" in ch.properties:
                    try:
                        val = await client.read_gatt_char(ch)
                        log(f"      gelesen: {hexdump(bytes(val))}")
                    except Exception as e:  # noqa: BLE001
                        log(f"      lesen fehlgeschlagen: {e}")
                for d in ch.descriptors:
                    log(f"      Deskriptor {d.uuid}  handle={d.handle}")
                if "notify" in ch.properties or "indicate" in ch.properties:
                    notify_chars.append(ch)
                if "write" in ch.properties or "write-without-response" in ch.properties:
                    write_chars.append(ch)

        def make_handler(uuid):
            def handler(_sender, data: bytearray):
                log(f"<< {uuid[:8]}  {len(data):3d} B  {hexdump(bytes(data))}")
            return handler

        log("=== Benachrichtigungen einschalten ===")
        for ch in notify_chars:
            try:
                await client.start_notify(ch, make_handler(ch.uuid))
                log(f"   an: {ch.uuid}")
            except Exception as e:  # noqa: BLE001
                log(f"   FEHLER bei {ch.uuid}: {e}")

        # Ziel fuer eigene Anfragen: bevorzugt der bekannte RX-Kanal
        target = next((c for c in write_chars if c.uuid.lower() == MCHP_RX_WRITE), None)
        if target is None and write_chars:
            target = next((c for c in write_chars if c.uuid.lower().startswith("49535343")), write_chars[0])

        async def tx(payload: bytes):
            if target is None:
                log("Kein beschreibbarer Kanal gefunden.")
                return
            resp = "write" in target.properties
            log(f">> {target.uuid[:8]}  {hexdump(payload)}")
            await client.write_gatt_char(target, payload, response=resp)

        log(f"=== {min(listen_s, 10)} s passiv zuhoeren (sendet der Regler von sich aus?) ===")
        await asyncio.sleep(min(listen_s, 10))

        if probe:
            log("=== Lese-Anfragen (Modbus RTU, nur Funktion 03/04) ===")
            for slave in (0x01, 0xFF):
                for func in (0x03, 0x04):
                    for start in (0x0000, 0x0100):
                        req = bytes([slave, func, start >> 8, start & 0xFF, 0x00, 0x10])
                        await tx(req + modbus_crc(req))
                        await asyncio.sleep(2)

        rest = max(0, listen_s - 10)
        if rest:
            log(f"=== weitere {rest} s zuhoeren ===")
            await asyncio.sleep(rest)

        for ch in notify_chars:
            try:
                await client.stop_notify(ch)
            except Exception:  # noqa: BLE001
                pass
    log("Getrennt. Fertig.")


def main():
    ap = argparse.ArgumentParser(description="Silentwind/Win-Power per BLE erkunden")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--address", default=DEFAULT_ADDRESS)
    ap.add_argument("--listen", type=int, default=30, help="Sekunden zuhoeren (Standard 30)")
    ap.add_argument("--probe", action="store_true", help="Modbus-Leseanfragen senden (mit Rueckfrage)")
    a = ap.parse_args()
    if a.scan:
        asyncio.run(scan())
        return
    if a.probe:
        print("--probe sendet Modbus-LESE-Anfragen (Funktion 03/04) an den Regler.")
        print("Das Protokoll des Reglers ist unbekannt; ein Restrisiko bleibt.")
        if input("Wirklich senden? Bitte 'ja' eingeben: ").strip().lower() != "ja":
            print("Abgebrochen - es wird nur zugehoert.")
            a.probe = False
    asyncio.run(explore(a.address, a.listen, a.probe))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
