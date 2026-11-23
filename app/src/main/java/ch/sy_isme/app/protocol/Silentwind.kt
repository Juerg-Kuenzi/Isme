package ch.sy_isme.app.protocol

/**
 * Silentwind-Hybridregler (Win-Power, Bluetooth-Modul ISSC BT5050), Protokoll wie in
 * tools/bluetooth/silentwind_reader.py, entschluesselt aus einem Mitschnitt der Hersteller-App.
 *
 * Rahmen: EB 90 EB 90 | Adresse | Befehl | 00 | 20 | 32 Datenbytes | Summe (2 Byte, big endian).
 * Die Summe ist die 16-Bit-Summe der Bytes ab Adresse bis zum Ende der Daten.
 */
data class WindData(
    val batteryVolt: Double,
    val windVolt: Double,
    val windAmp: Double,
    val windWatt: Double,
    /** Berechnet wie in der Hersteller-App: Windleistung / Batteriespannung */
    val batteryAmp: Double,
    val brakeActive: Boolean,
)

class SilentwindFrame(val address: Int, val command: Int, val data: ByteArray)

object Silentwind {
    const val FRAME_LENGTH = 42
    const val DATA_LENGTH = 32

    const val CMD_LIVE = 0x00
    const val CMD_READ_SETTINGS = 0x01
    const val CMD_WRITE_SETTINGS = 0x04
    const val RESP_LIVE = 0x06
    const val RESP_SETTINGS = 0x07
    const val RESP_WRITE = 0x09

    /** Einstellungsblock Byte 1: Bremse (0x30 = aus, 0x34 = ein) */
    const val BRAKE_BIT = 0x04

    /** Live-Werte Byte 0: Bremse aktiv */
    const val LIVE_BRAKE_BIT = 0x40

    val HEADER = byteArrayOf(0xEB.toByte(), 0x90.toByte(), 0xEB.toByte(), 0x90.toByte())

    fun checksum(bytes: ByteArray, from: Int, to: Int): Int {
        var sum = 0
        for (i in from until to) sum += bytes[i].toInt() and 0xFF
        return sum and 0xFFFF
    }

    /** Anfrage der App, Adresse 00 */
    fun request(command: Int, data: ByteArray = ByteArray(DATA_LENGTH)): ByteArray {
        require(data.size == DATA_LENGTH) { "32 Datenbytes erwartet" }
        val frame = ByteArray(FRAME_LENGTH)
        HEADER.copyInto(frame)
        frame[4] = 0x00
        frame[5] = command.toByte()
        frame[6] = 0x00
        frame[7] = 0x20
        data.copyInto(frame, 8)
        val sum = checksum(frame, 4, 40)
        frame[40] = (sum shr 8).toByte()
        frame[41] = sum.toByte()
        return frame
    }

    /** 12-Bit-Wert, linksbuendig in zwei Bytes: aus 86 00 wird 0x860 */
    private fun w12(d: ByteArray, i: Int): Int = ((d[i].toInt() and 0xFF) shl 4) or ((d[i + 1].toInt() and 0xFF) shr 4)

    fun decodeLive(d: ByteArray): WindData {
        require(d.size >= DATA_LENGTH)
        val battery = w12(d, 2) / 80.0
        val windPower = (((d[8].toInt() and 0xFF) shl 16) or ((d[9].toInt() and 0xFF) shl 8) or
            (d[10].toInt() and 0xFF)) / 6400.0
        return WindData(
            batteryVolt = battery,
            windVolt = w12(d, 4) / 80.0,
            windAmp = w12(d, 14) / 80.0,
            windWatt = windPower,
            batteryAmp = if (battery > 0) windPower / battery else 0.0,
            brakeActive = (d[0].toInt() and LIVE_BRAKE_BIT) != 0,
        )
    }

    /** Ein leerer oder offensichtlich ungueltiger Block darf nie zurueckgeschrieben werden. */
    fun isUsableSettings(block: ByteArray): Boolean =
        block.size == DATA_LENGTH && block[0].toInt() != 0 && block.any { it.toInt() != 0 }

    fun brakeOn(block: ByteArray): Boolean = (block[1].toInt() and BRAKE_BIT) != 0

    /**
     * Kopie des eben gelesenen Blocks, in der nur das Bremsen-Bit geaendert ist.
     * Der Regler kennt nur den ganzen Block, alle anderen Bytes muessen unveraendert bleiben.
     */
    fun withBrake(block: ByteArray, on: Boolean): ByteArray {
        require(isUsableSettings(block)) { "Einstellungsblock ungültig" }
        val result = block.copyOf()
        val b1 = block[1].toInt() and 0xFF
        result[1] = (if (on) b1 or BRAKE_BIT else b1 and BRAKE_BIT.inv()).toByte()
        val changed = result.indices.filter { result[it] != block[it] }
        check(changed.isEmpty() || changed == listOf(1)) { "unerwartete Änderung $changed" }
        return result
    }
}

/** Setzt Rahmen aus den Bluetooth-Paketen zusammen und verwirft solche mit falscher Summe. */
class SilentwindFrameCollector {
    private var buffer = ByteArray(0)

    fun feed(bytes: ByteArray): List<SilentwindFrame> {
        buffer += bytes
        val frames = mutableListOf<SilentwindFrame>()
        while (true) {
            val start = indexOfHeader()
            if (start < 0) {
                // Die letzten drei Bytes koennten der Anfang eines Kopfes sein.
                if (buffer.size > 3) buffer = buffer.copyOfRange(buffer.size - 3, buffer.size)
                return frames
            }
            if (start > 0) buffer = buffer.copyOfRange(start, buffer.size)
            if (buffer.size < Silentwind.FRAME_LENGTH) return frames
            val frame = buffer.copyOfRange(0, Silentwind.FRAME_LENGTH)
            buffer = buffer.copyOfRange(Silentwind.FRAME_LENGTH, buffer.size)
            val sum = Silentwind.checksum(frame, 4, 40)
            val given = ((frame[40].toInt() and 0xFF) shl 8) or (frame[41].toInt() and 0xFF)
            if (sum == given) {
                frames += SilentwindFrame(frame[4].toInt() and 0xFF, frame[5].toInt() and 0xFF, frame.copyOfRange(8, 40))
            }
        }
    }

    private fun indexOfHeader(): Int {
        val h = Silentwind.HEADER
        for (i in 0..buffer.size - h.size) {
            if (buffer[i] == h[0] && buffer[i + 1] == h[1] && buffer[i + 2] == h[2] && buffer[i + 3] == h[3]) return i
        }
        return -1
    }
}
