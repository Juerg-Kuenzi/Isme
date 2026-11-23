package ch.sy_isme.app.net

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.content.Context
import android.os.Build
import android.os.SystemClock
import ch.sy_isme.app.protocol.Silentwind
import ch.sy_isme.app.protocol.SilentwindFrame
import ch.sy_isme.app.protocol.SilentwindFrameCollector
import ch.sy_isme.app.protocol.WindData
import java.io.IOException
import java.util.UUID
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Bluetooth-LE-Verbindung zum Silentwind-Regler ueber den Microchip-Dienst "Transparent UART".
 *
 * Der Regler erlaubt nur eine Verbindung gleichzeitig. Die Verbindung soll deshalb nur so kurz wie
 * noetig bestehen: [stop] trennt sofort und gibt den Regler fuer andere Geraete frei.
 *
 * Alle Bluetooth-Vorgaenge laufen nacheinander auf einem eigenen Thread. Die Listener-Methoden werden
 * auf diesem Thread aufgerufen. Die Berechtigung BLUETOOTH_CONNECT prueft der Aufrufer vor [start].
 */
@SuppressLint("MissingPermission")
class SilentwindLink(
    context: Context,
    private val address: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onStateChanged(state: State, reason: String?)
        fun onLive(data: WindData)
        fun onBrakeResult(result: BrakeResult)
    }

    enum class State { CONNECTING, CONNECTED, CLOSED }

    sealed interface BrakeResult {
        data class Confirmed(val on: Boolean) : BrakeResult
        data class AlreadySet(val on: Boolean) : BrakeResult
        data class NotConfirmed(val on: Boolean) : BrakeResult
        data class Failed(val reason: String) : BrakeResult
    }

    private sealed interface Event {
        data class Connection(val connected: Boolean, val status: Int) : Event
        data class Services(val status: Int) : Event
        data class Mtu(val mtu: Int, val status: Int) : Event
        data class DescriptorWritten(val status: Int) : Event
        data class Written(val status: Int) : Event
    }

    private val appContext = context.applicationContext
    private val events = LinkedBlockingQueue<Event>()
    private val frames = LinkedBlockingQueue<SilentwindFrame>()
    private val collector = SilentwindFrameCollector()
    private val wake = LinkedBlockingQueue<Unit>()
    private val gattLock = Any()

    @Volatile private var running = false
    @Volatile private var lost = false
    @Volatile private var pendingBrake: Boolean? = null
    private var thread: Thread? = null
    private var gatt: BluetoothGatt? = null
    private var writeChar: BluetoothGattCharacteristic? = null
    private var chunkSize = DEFAULT_CHUNK

    fun start() {
        if (running) return
        running = true
        thread = Thread(::run, "silentwind").apply {
            isDaemon = true
            start()
        }
    }

    /** Trennt sofort und gibt den Regler frei. */
    fun stop() {
        running = false
        thread?.interrupt()
        thread = null
        closeGatt()
    }

    /** Schaltet die Bremse beim naechsten Durchgang des Bluetooth-Threads. */
    fun requestBrake(on: Boolean) {
        pendingBrake = on
        wake.offer(Unit)
    }

    private fun run() {
        var reason: String? = null
        try {
            listener.onStateChanged(State.CONNECTING, null)
            connect()
            listener.onStateChanged(State.CONNECTED, null)
            var misses = 0
            while (running) {
                val started = SystemClock.elapsedRealtime()
                pendingBrake?.let { on ->
                    pendingBrake = null
                    listener.onBrakeResult(setBrake(on))
                }
                val live = transact(Silentwind.request(Silentwind.CMD_LIVE), Silentwind.RESP_LIVE)
                if (live == null) {
                    if (++misses >= 3) throw IOException("Der Regler antwortet nicht.")
                } else {
                    misses = 0
                    listener.onLive(Silentwind.decodeLive(live.data))
                }
                val rest = POLL_MS - (SystemClock.elapsedRealtime() - started)
                if (rest > 0) wake.poll(rest, TimeUnit.MILLISECONDS)
            }
        } catch (_: InterruptedException) {
            // stop() wurde aufgerufen
        } catch (e: Exception) {
            if (running) reason = e.message ?: e.toString()
        } finally {
            closeGatt()
            running = false
            pendingBrake?.let {
                pendingBrake = null
                listener.onBrakeResult(BrakeResult.Failed(reason ?: "Verbindung getrennt."))
            }
            listener.onStateChanged(State.CLOSED, reason)
        }
    }

    // ------------------------------------------------------------------
    // Bremse: nur einen eben gelesenen Block zurueckschreiben, nur ein Bit aendern
    // ------------------------------------------------------------------

    private fun setBrake(on: Boolean): BrakeResult {
        var lastError = "Der Regler antwortet nicht."
        repeat(BRAKE_ATTEMPTS) {
            try {
                return trySetBrake(on)
            } catch (e: IOException) {
                if (lost) throw e
                lastError = e.message ?: lastError
            }
        }
        return BrakeResult.Failed(lastError)
    }

    private fun trySetBrake(on: Boolean): BrakeResult {
        // 1. aktuellen Einstellungsblock frisch lesen
        val settings = transact(Silentwind.request(Silentwind.CMD_READ_SETTINGS), Silentwind.RESP_SETTINGS)
            ?: throw IOException("Keine Einstellungen vom Regler erhalten.")
        val current = settings.data
        if (!Silentwind.isUsableSettings(current)) {
            return BrakeResult.Failed("Der Regler hat leere Einstellungen gemeldet. Es wurde nichts geschrieben.")
        }
        if (Silentwind.brakeOn(current) == on) return BrakeResult.AlreadySet(on)

        // 2. nur das Bremsen-Bit aendern und den ganzen Block zurueckschreiben
        val changed = Silentwind.withBrake(current, on)
        val echo = transact(Silentwind.request(Silentwind.CMD_WRITE_SETTINGS, changed), Silentwind.RESP_WRITE)
            ?: throw IOException("Der Regler hat das Schreiben nicht bestätigt.")
        if (!echo.data.contentEquals(changed)) {
            return BrakeResult.Failed("Der Regler hat andere Einstellungen bestätigt. Bitte prüfen.")
        }

        // 3. Zustand in den Live-Werten pruefen
        repeat(5) {
            transact(Silentwind.request(Silentwind.CMD_LIVE), Silentwind.RESP_LIVE)?.let { frame ->
                val data = Silentwind.decodeLive(frame.data)
                listener.onLive(data)
                if (data.brakeActive == on) return BrakeResult.Confirmed(on)
            }
            Thread.sleep(1000)
        }
        return BrakeResult.NotConfirmed(on)
    }

    // ------------------------------------------------------------------
    // Bluetooth
    // ------------------------------------------------------------------

    private fun connect() {
        val manager = appContext.getSystemService(BluetoothManager::class.java)
        val adapter = manager?.adapter ?: throw IOException("Dieses Gerät hat kein Bluetooth.")
        if (!adapter.isEnabled) throw IOException("Bluetooth ist ausgeschaltet.")
        val device = adapter.getRemoteDevice(address)

        var attempt = 0
        while (true) {
            lost = false
            events.clear()
            val g = device.connectGatt(appContext, false, callback, BluetoothDevice.TRANSPORT_LE)
            synchronized(gattLock) {
                if (!running) {
                    g.close()
                    throw InterruptedException()
                }
                gatt = g
            }
            val ev = awaitConnection()
            if (ev?.connected == true) break
            closeGatt()
            if (++attempt >= CONNECT_ATTEMPTS) {
                throw IOException("Regler nicht erreichbar. Ausser Reichweite oder mit einem anderen Gerät verbunden?")
            }
            Thread.sleep(1000)
        }
        val g = gatt ?: throw InterruptedException()

        // Manche Geraete (z. B. Samsung mit Android 11) liefern direkt nach dem Verbinden oder aus
        // ihrem Zwischenspeicher eine veraltete Dienstliste. Dann den Speicher leeren und neu fragen.
        Thread.sleep(SETTLE_MS)
        discoverServices(g)
        var notifyChar = g.services.firstNotNullOfOrNull { it.getCharacteristic(NOTIFY_UUID) }
        writeChar = g.services.firstNotNullOfOrNull { it.getCharacteristic(WRITE_UUID) }
        if (notifyChar == null || writeChar == null) {
            refreshServiceCache(g)
            Thread.sleep(REFRESH_WAIT_MS)
            discoverServices(g)
            notifyChar = g.services.firstNotNullOfOrNull { it.getCharacteristic(NOTIFY_UUID) }
            writeChar = g.services.firstNotNullOfOrNull { it.getCharacteristic(WRITE_UUID) }
        }
        if (notifyChar == null || writeChar == null) {
            val found = g.services.joinToString(", ") { shortUuid(it.uuid) }.ifEmpty { "keine" }
            throw IOException(
                "Der Regler bietet den erwarteten Bluetooth-Dienst nicht an (gefunden: $found). " +
                    "Bluetooth aus- und einschalten oder die Kopplung mit dem Regler aufheben hilft oft.",
            )
        }

        // Ein Rahmen hat 42 Bytes. Gelingt eine groessere MTU nicht, wird in Stuecken gesendet.
        chunkSize = DEFAULT_CHUNK
        if (g.requestMtu(REQUESTED_MTU)) {
            try {
                val mtu = await<Event.Mtu>(MTU_TIMEOUT_MS)
                if (mtu.status == BluetoothGatt.GATT_SUCCESS) chunkSize = (mtu.mtu - 3).coerceAtLeast(DEFAULT_CHUNK)
            } catch (_: IOException) {
                if (lost) throw IOException("Verbindung zum Regler verloren.")
            }
        }

        if (!g.setCharacteristicNotification(notifyChar, true)) throw IOException("Benachrichtigungen nicht möglich.")
        val cccd = notifyChar.getDescriptor(CCCD_UUID) ?: throw IOException("Benachrichtigungen nicht möglich.")
        val value = if (notifyChar.properties and BluetoothGattCharacteristic.PROPERTY_NOTIFY != 0) {
            BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        } else {
            BluetoothGattDescriptor.ENABLE_INDICATION_VALUE
        }
        val started = if (Build.VERSION.SDK_INT >= 33) {
            g.writeDescriptor(cccd, value) == BluetoothStatusCodes.SUCCESS
        } else {
            @Suppress("DEPRECATION")
            cccd.value = value
            @Suppress("DEPRECATION")
            g.writeDescriptor(cccd)
        }
        if (!started || await<Event.DescriptorWritten>(OP_TIMEOUT_MS).status != BluetoothGatt.GATT_SUCCESS) {
            throw IOException("Benachrichtigungen nicht möglich.")
        }
    }

    private fun discoverServices(g: BluetoothGatt) {
        events.removeIf { it is Event.Services }
        if (!g.discoverServices()) throw IOException("Dienste des Reglers nicht lesbar.")
        val services = await<Event.Services>(SERVICES_TIMEOUT_MS)
        if (services.status != BluetoothGatt.GATT_SUCCESS) throw IOException("Dienste des Reglers nicht lesbar.")
    }

    /**
     * Leert Androids Zwischenspeicher der Dienstliste. Die Methode ist nicht oeffentlich, wird aber
     * von vielen BLE-Bibliotheken genutzt. Fehlt sie, bleibt es beim zweiten Versuch ohne Leeren.
     */
    @SuppressLint("DiscouragedPrivateApi", "PrivateApi", "SoonBlockedPrivateApi")
    private fun refreshServiceCache(g: BluetoothGatt) {
        try {
            g.javaClass.getMethod("refresh").invoke(g)
        } catch (_: Exception) {
            // nicht verfuegbar
        }
    }

    /** 0000180a-0000-1000-8000-00805f9b34fb wird zu 180a, eigene UUIDs bleiben gekuerzt erkennbar */
    private fun shortUuid(uuid: UUID): String {
        val s = uuid.toString()
        return if (s.endsWith("-0000-1000-8000-00805f9b34fb") && s.startsWith("0000")) s.substring(4, 8) else s.substring(0, 13)
    }

    /** Sendet eine Anfrage und wartet auf die Antwort mit dem erwarteten Befehl. Null bei Zeitablauf. */
    private fun transact(request: ByteArray, response: Int): SilentwindFrame? {
        frames.clear()
        write(request)
        val deadline = SystemClock.elapsedRealtime() + RESPONSE_TIMEOUT_MS
        while (true) {
            val left = deadline - SystemClock.elapsedRealtime()
            if (left <= 0) return null
            val frame = frames.poll(left, TimeUnit.MILLISECONDS) ?: return null
            if (lost) throw IOException("Verbindung zum Regler verloren.")
            if (frame.command == response) return frame
        }
    }

    private fun write(bytes: ByteArray) {
        val g = gatt ?: throw InterruptedException()
        val ch = writeChar ?: throw IOException("Nicht verbunden.")
        var offset = 0
        while (offset < bytes.size) {
            val part = bytes.copyOfRange(offset, minOf(bytes.size, offset + chunkSize))
            events.removeIf { it is Event.Written }
            val accepted = if (Build.VERSION.SDK_INT >= 33) {
                g.writeCharacteristic(ch, part, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) ==
                    BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                @Suppress("DEPRECATION")
                ch.value = part
                @Suppress("DEPRECATION")
                g.writeCharacteristic(ch)
            }
            if (!accepted) throw IOException("Senden an den Regler abgelehnt.")
            if (await<Event.Written>(OP_TIMEOUT_MS).status != BluetoothGatt.GATT_SUCCESS) {
                throw IOException("Senden an den Regler fehlgeschlagen.")
            }
            offset += part.size
        }
    }

    private fun awaitConnection(): Event.Connection? {
        val deadline = SystemClock.elapsedRealtime() + CONNECT_TIMEOUT_MS
        while (true) {
            val left = deadline - SystemClock.elapsedRealtime()
            if (left <= 0) return null
            val ev = events.poll(left, TimeUnit.MILLISECONDS) ?: return null
            if (ev is Event.Connection) return ev
        }
    }

    private inline fun <reified T : Event> await(timeoutMs: Long): T {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (true) {
            val left = deadline - SystemClock.elapsedRealtime()
            if (left <= 0) throw IOException("Der Regler antwortet nicht.")
            val ev = events.poll(left, TimeUnit.MILLISECONDS) ?: throw IOException("Der Regler antwortet nicht.")
            if (ev is Event.Connection && !ev.connected) throw IOException("Verbindung zum Regler verloren.")
            if (ev is T) return ev
        }
    }

    private fun closeGatt() {
        synchronized(gattLock) {
            gatt?.let {
                it.disconnect()
                it.close()
            }
            gatt = null
        }
    }

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            val connected = newState == BluetoothProfile.STATE_CONNECTED && status == BluetoothGatt.GATT_SUCCESS
            if (!connected) {
                lost = true
                frames.offer(SilentwindFrame(-1, -1, ByteArray(0)))   // weckt wartende Anfragen
            }
            events.offer(Event.Connection(connected, status))
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            events.offer(Event.Services(status))
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            events.offer(Event.Mtu(mtu, status))
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            events.offer(Event.DescriptorWritten(status))
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            events.offer(Event.Written(status))
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            if (c.uuid == NOTIFY_UUID) receive(value)
        }

        @Deprecated("Bis Android 12")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            if (Build.VERSION.SDK_INT < 33 && c.uuid == NOTIFY_UUID) {
                @Suppress("DEPRECATION")
                receive(c.value ?: return)
            }
        }
    }

    private fun receive(value: ByteArray) {
        synchronized(collector) { collector.feed(value) }.forEach(frames::offer)
    }

    companion object {
        /** MAC-Adresse des Reglers an Bord, wie in tools/bluetooth/silentwind_reader.py */
        const val DEFAULT_ADDRESS = "34:81:F4:82:77:78"

        val NOTIFY_UUID: UUID = UUID.fromString("49535343-1e4d-4bd9-ba61-23c647249616")
        val WRITE_UUID: UUID = UUID.fromString("49535343-8841-43f4-a8d4-ecbe34729bb3")
        val CCCD_UUID: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        private const val POLL_MS = 1000L
        private const val CONNECT_ATTEMPTS = 2
        private const val BRAKE_ATTEMPTS = 3
        private const val CONNECT_TIMEOUT_MS = 20_000L
        private const val SERVICES_TIMEOUT_MS = 10_000L
        private const val SETTLE_MS = 600L
        private const val REFRESH_WAIT_MS = 1_000L
        private const val MTU_TIMEOUT_MS = 5_000L
        private const val OP_TIMEOUT_MS = 3_000L
        private const val RESPONSE_TIMEOUT_MS = 3_000L
        private const val REQUESTED_MTU = 64
        private const val DEFAULT_CHUNK = 20
    }
}
