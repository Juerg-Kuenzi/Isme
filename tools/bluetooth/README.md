# Bluetooth-Skripte

Python-Skripte zum Auslesen der Bord-Geräte per Bluetooth LE vom Laptop oder Bordrechner, ohne die Hersteller-Apps. Getestet unter Windows mit Python 3.10 oder neuer.

```
pip install bleak
```

Die jeweilige Hersteller-App vorher auf allen Handys schliessen. Beide Geräte erlauben nur eine Bluetooth-Verbindung gleichzeitig.

| Datei | Zweck |
|---|---|
| `daly_ble_reader.py` | Die drei Daly-BMS der Batterien auslesen, optional als CSV |
| `silentwind_reader.py` | Silentwind-Laderegler auslesen und Bremse schalten |
| `Bremse_EIN.bat`, `Bremse_AUS.bat` | Bremse per Doppelklick, im selben Ordner wie `silentwind_reader.py` |
| `silentwind_ble_explore.py` | Unbekanntes BLE-Gerät erkunden: Kanäle auflisten und zuhören |

## Daly-BMS

```
py daly_ble_reader.py --scan
py daly_ble_reader.py --cells
py daly_ble_reader.py --interval 60 --csv bms_log.csv
```

- Die MAC-Adressen der drei BMS stehen oben im Skript in `BMS_LIST`.
- Das Skript verbindet sich, liest und trennt sofort wieder. Zwischen den Abfragen bleibt jedes BMS für die Daly-App frei.
- Die CSV hat eine Zeile pro Batterie und Messung, mit Dezimalpunkt und `;` als Trennzeichen.

**Protokoll:** Neues Daly-Protokoll (Modbus). Benachrichtigungen kommen auf `FFF1`, Anfragen gehen an `FFF2`. Die Anfrage `D2 03 00 00 00 3E D7 B9` liest 62 Register. Die Antwort beginnt mit `D2 03 7C` und endet mit einer Modbus-CRC.

| Register | Inhalt |
|---|---|
| 0 bis 31 | Zellspannungen in mV |
| 32 bis 39 | Temperaturen in °C, minus 40 |
| 40 | Gesamtspannung, 0,1 V |
| 41 | Strom, (Wert − 30000) × 0,1 A, negativ heisst Entladen |
| 42 | SOC, 0,1 % |
| 43, 44 | höchste und tiefste Zellspannung, mV |
| 45, 46 | höchste und tiefste Temperatur, minus 40 |
| 48 | Restkapazität, 0,1 Ah |
| 49, 50, 51 | Zellenzahl, Anzahl Temperaturfühler, Ladezyklen |
| 53, 54 | MOSFET Laden und Entladen |
| 55, 56, 57 | mittlere Zellspannung, Zelldifferenz in mV, Leistung in W |

Unsicher sind Register 47 (Status), 56 und 57, ebenso die Bedeutung von 58 bis 61 (vermutlich Alarm-Bits).

## Silentwind-Laderegler

```
py silentwind_reader.py --interval 60 --kurz --csv wind.csv
py silentwind_reader.py --bremse ein
py silentwind_reader.py --bremse aus --ja
```

- `--kurz` trennt nach jeder Messung. Bricht die Verbindung ab, verbindet sich das Skript nach 10 s neu.
- `--bremse` fragt nach, ausser mit `--ja`. Bei schwachem Empfang versucht es das Skript bis zu dreimal.
- Ohne `--bremse` sendet das Skript nur Leseanfragen.

**Wichtig:** Zeigt die Silentwind-App leere Werte (0.0), zum Beispiel auf dem Fairphone, dort weder „Save Data“ noch „Manual Brake“ verwenden. Die App würde den leeren Einstellungsblock in den Regler schreiben.

**Protokoll:** Der Regler hat ein Bluetooth-Modul ISSC BT5050, das klassisches SPP und BLE kann. Die Hersteller-App nutzt SPP. Das Skript nutzt BLE mit dem Microchip-Dienst „Transparent UART“ und sendet dieselben Bytes. Anfragen gehen an `49535343-8841-…`, Antworten kommen auf `49535343-1e4d-…`.

Jeder Rahmen hat 42 Bytes:

```
EB 90 EB 90 | Adresse | Befehl | 00 | 20 | 32 Datenbytes | Summe (2 Byte, big endian)
```

Die Summe ist die 16-Bit-Summe der Bytes ab „Adresse“ bis zum Ende der Daten. Die App sendet mit Adresse `00`, der Regler antwortet mit `29`.

| Befehl | Antwort | Inhalt |
|---|---|---|
| `00` | `06` | Live-Werte, die App fragt jede Sekunde |
| `01` | `07` | Einstellungsblock lesen |
| `04` + Block | `09` (Echo) | Einstellungsblock schreiben |

Live-Werte (Datenbytes ab 0). Ein „12-Bit-Wert“ steht linksbündig in zwei Bytes, aus `86 00` wird zum Beispiel `0x860`.

| Bytes | Inhalt |
|---|---|
| 0 | Bit `0x40`: Bremse aktiv |
| 2–3 | Batteriespannung, 12-Bit-Wert ÷ 80 V |
| 4–5 | Windspannung, 12-Bit-Wert ÷ 80 V |
| 7 | unbekannt, fällt beim Bremsen auf 0 (Drehzahl?) |
| 8–10 | Windleistung, 24 Bit ÷ 6400 W (= Rohspannung × Rohstrom) |
| 14–15 | Windstrom, 12-Bit-Wert ÷ 80 A |

Die Faktoren sind mit Screenshots der App abgeglichen. Die App schneidet die zweite Nachkommastelle ab. Den Batteriestrom zeigt die App als Windleistung geteilt durch Batteriespannung. Die Solar-Felder sind noch nicht bestimmt, weil im Mitschnitt kein Solarstrom floss.

**Bremse:** Byte 1 des Einstellungsblocks, Bit `0x04` (`30` = aus, `34` = ein). Die App liest den Block, ändert das Bit und schreibt den ganzen Block mit Befehl `04` zurück. Das Skript macht dasselbe. Es schreibt nur einen Block, den es unmittelbar vorher mit gültiger Summe gelesen hat, ändert nur dieses Bit und prüft danach das Echo und das Bit `0x40` in den Live-Werten.

Der Rest des Einstellungsblocks ist nicht entschlüsselt. Das Skript schreibt ihn unverändert zurück.

**Mitschnitt erstellen:** Auf einem Android-Handy, auf dem die App funktioniert, in den Entwickleroptionen den „Bluetooth HCI-Snoop-Log“ auf „Aktiviert“ stellen. Bluetooth aus- und einschalten, die App benutzen und dann „Fehlerbericht erstellen“ wählen. Der Mitschnitt liegt in der ZIP-Datei unter `FS/data/misc/bluetooth/logs/btsnoop_hci.log`.
