package ch.sy_isme.app

import ch.sy_isme.app.protocol.Silentwind
import ch.sy_isme.app.protocol.SilentwindFrameCollector
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class SilentwindTest {
    private fun hex(s: String): ByteArray = s.replace(" ", "").chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    /** Antwort des Reglers (Adresse 29) mit gueltiger Summe */
    private fun response(command: Int, data: ByteArray): ByteArray {
        val f = Silentwind.request(command, data)
        f[4] = 0x29
        val sum = Silentwind.checksum(f, 4, 40)
        f[40] = (sum shr 8).toByte()
        f[41] = sum.toByte()
        return f
    }

    @Test fun requestsMatchTheReaderScript() {
        val live = Silentwind.request(Silentwind.CMD_LIVE)
        val settings = Silentwind.request(Silentwind.CMD_READ_SETTINGS)
        assertEquals(42, live.size)
        assertArrayEquals(hex("EB90EB90 00000020" + "00".repeat(32) + "0020"), live)
        assertArrayEquals(hex("EB90EB90 00010020" + "00".repeat(32) + "0021"), settings)
    }

    @Test fun decodesLiveValuesLikeTheApp() {
        // Abgleich aus dem README: Batterie 26.8 V, Wind 10.8 V / 1.55 A / 16.7 W
        val d = ByteArray(32)
        d[0] = 0x00
        hex("8600").copyInto(d, 2)       // 0x860 = 2144 -> 26.8 V
        hex("3600").copyInto(d, 4)       // 0x360 = 864  -> 10.8 V
        hex("01A180").copyInto(d, 8)     // 106880 / 6400 = 16.7 W
        hex("07C0").copyInto(d, 14)      // 0x07C = 124  -> 1.55 A
        val v = Silentwind.decodeLive(d)
        assertEquals(26.8, v.batteryVolt, 1e-9)
        assertEquals(10.8, v.windVolt, 1e-9)
        assertEquals(1.55, v.windAmp, 1e-9)
        assertEquals(16.7, v.windWatt, 1e-9)
        assertEquals(16.7 / 26.8, v.batteryAmp, 1e-9)
        assertFalse(v.brakeActive)
        d[0] = 0x40
        assertTrue(Silentwind.decodeLive(d).brakeActive)
        assertEquals(0.0, Silentwind.decodeLive(ByteArray(32)).batteryAmp, 0.0)
    }

    @Test fun collectsFramesAcrossPacketsAndDropsBadOnes() {
        val good = response(Silentwind.RESP_LIVE, ByteArray(32).also { it[0] = 0x40 })
        val bad = response(Silentwind.RESP_SETTINGS, ByteArray(32)).also { it[41] = (it[41] + 1).toByte() }
        val stream = hex("0102EB") + bad + good
        val c = SilentwindFrameCollector()
        val frames = stream.toList().chunked(20).flatMap { c.feed(it.toByteArray()) }
        assertEquals(1, frames.size)
        assertEquals(0x29, frames[0].address)
        assertEquals(Silentwind.RESP_LIVE, frames[0].command)
        assertEquals(0x40, frames[0].data[0].toInt())
    }

    @Test fun brakeChangesOnlyTheOneBit() {
        val block = ByteArray(32) { (it * 7 + 3).toByte() }
        block[0] = 0x11
        block[1] = 0x30
        assertFalse(Silentwind.brakeOn(block))
        val on = Silentwind.withBrake(block, true)
        assertEquals(0x34, on[1].toInt())
        assertTrue(Silentwind.brakeOn(on))
        for (i in block.indices) if (i != 1) assertEquals(block[i], on[i])
        val off = Silentwind.withBrake(on, false)
        assertArrayEquals(block, off)
        assertArrayEquals(on, Silentwind.withBrake(on, true))
        assertEquals(0x30, block[1].toInt())   // Original bleibt unveraendert
    }

    @Test fun refusesEmptyOrInvalidBlocks() {
        assertFalse(Silentwind.isUsableSettings(ByteArray(32)))
        assertFalse(Silentwind.isUsableSettings(ByteArray(32).also { it[1] = 0x30 }))   // Byte 0 leer
        assertFalse(Silentwind.isUsableSettings(ByteArray(31) { 1 }))
        try {
            Silentwind.withBrake(ByteArray(32), true)
            fail("leerer Block darf nicht verwendet werden")
        } catch (_: IllegalArgumentException) {
        }
    }

    @Test fun writeRequestCarriesTheModifiedBlock() {
        val block = ByteArray(32) { 0x22 }.also { it[1] = 0x30 }
        val req = Silentwind.request(Silentwind.CMD_WRITE_SETTINGS, Silentwind.withBrake(block, true))
        assertEquals(Silentwind.CMD_WRITE_SETTINGS, req[5].toInt())
        assertEquals(0x34, req[9].toInt())
        val sum = Silentwind.checksum(req, 4, 40)
        assertEquals(sum, ((req[40].toInt() and 0xFF) shl 8) or (req[41].toInt() and 0xFF))
    }
}
