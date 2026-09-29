package com.madboly.netusage.data

import android.content.Context
import android.net.ConnectivityManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.URL
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Pulls the laptop's usage from its collector over the home network. The laptop files Wi-Fi
 * under the same router keys as this phone, so a shared router adds up across both.
 *
 * The token (the laptop's /var/lib/netusage/share.token) is never sent: each request is
 * signed with it over a fresh nonce and the time, and the laptop signs its reply over the
 * same nonce. Searching the network therefore gives nothing reusable to whatever answers,
 * and a device pretending to be the laptop cannot feed in numbers.
 */
object Laptop {
    private const val PORT = 8765

    fun configured(prefs: Prefs) = prefs.laptopToken.isNotEmpty()

    /** Returns null on success, or what went wrong. */
    @Synchronized
    fun sync(context: Context): String? {
        val prefs = Prefs(context)
        if (!configured(prefs)) return "not set up"
        val db = Db.get(context)
        var host = prefs.laptopHost
        val dev = if (prefs.isMember) prefs.memberId else ""
        if (host.isEmpty() || ping(host, prefs.laptopToken, dev) == null) {
            // Searching the network is slow; while the laptop is away, try it now and then.
            val now = System.currentTimeMillis() / 1000
            val lastScan = db.meta("laptop_scan_at")?.toLongOrNull() ?: 0
            if (now - lastScan < 15 * 60) return "laptop not reachable"
            db.setMeta("laptop_scan_at", now.toString())
            host = find(context, prefs.laptopToken, dev) ?: return "laptop not found on this network"
            prefs.laptopHost = host
        }
        if (prefs.isMember) return syncMember(context, host, prefs)
        // Re-read from the start of the previous day, so the hour in progress, late counts
        // and whole-day rows from the laptop's older records are all replaced.
        val synced = db.meta("laptop_synced_to")?.toLongOrNull() ?: 0L
        // Rows stored before unnamed Wi-Fi was placed are taken again, once.
        val resumeFrom = if (synced <= 0 || db.meta("laptop_placed") == null) 0L else java.time.Instant.ofEpochSecond(synced - 86_400)
            .atZone(java.time.ZoneId.systemDefault()).toLocalDate()
            .atStartOfDay(java.time.ZoneId.systemDefault()).toEpochSecond()
        var since = resumeFrom
        var json = runCatching { get(host, "/v1/usage", prefs.laptopToken, 30_000, since) }
            .getOrElse { return it.message ?: "could not reach the laptop" }
        // A newer laptop may file its history differently: take all of it again.
        val version = json.optInt("version").toString()
        if (since > 0 && db.meta("laptop_version") != version) {
            since = 0
            json = runCatching { get(host, "/v1/usage", prefs.laptopToken, 60_000, 0) }
                .getOrElse { return it.message ?: "could not reach the laptop" }
        }
        val device = json.optString("device", "laptop")
        val w = db.writableDatabase
        w.beginTransaction()
        try {
            val nets = json.getJSONArray("networks")
            for (i in 0 until nets.length()) {
                val n = nets.getJSONObject(i)
                val kind = n.getString("kind")
                w.execSQL(
                    "INSERT OR IGNORE INTO networks (key, net, label, grp, names, last_seen) VALUES (?, ?, ?, ?, ?, 0)",
                    arrayOf(n.getString("key"), if (kind == WIFI) WIFI else WIRED, n.getString("label"),
                        n.getString("key"), n.getString("label"))
                )
            }
            w.execSQL("DELETE FROM remote_usage WHERE device = ? AND hour >= ?", arrayOf(device, since))
            val hours = json.getJSONArray("hours")
            val place = PhoneWifi(db, since)
            for (i in 0 until hours.length()) {
                val r = hours.getJSONArray(i)
                val net = r.getString(1)
                // The laptop shares this phone's Wi-Fi: what it could not name goes where the phone was.
                val name = r.getString(2).ifEmpty { if (net == WIFI) place.at(r.getLong(0)) else "" }
                // add rather than replace: two rows may land on one name (see Db.addSample)
                w.execSQL(
                    "INSERT OR IGNORE INTO remote_usage (device, hour, net, name, rx, tx) VALUES (?, ?, ?, ?, 0, 0)",
                    arrayOf(device, r.getLong(0), net, name)
                )
                w.execSQL(
                    "UPDATE remote_usage SET rx = rx + ?, tx = tx + ? WHERE device = ? AND hour = ? AND net = ? AND name = ?",
                    arrayOf(r.getLong(3), r.getLong(4), device, r.getLong(0), net, name)
                )
            }
            val now = json.optLong("now", System.currentTimeMillis() / 1000)
            db.setMeta("laptop_placed", "1")
            db.setMeta("laptop_synced_to", (now - now % 3600).toString())
            db.setMeta("laptop_last_sync", (System.currentTimeMillis() / 1000).toString())
            db.setMeta("laptop_device", device)
            db.setMeta("laptop_version", version)
            w.setTransactionSuccessful()
        } finally {
            w.endTransaction()
        }
        // an older laptop does not know about family phones yet; that must not stop the report
        val phones = pullPhones(context, host, prefs)
        return report(context, host, prefs) ?: phones
    }

    /** A family phone sends its own usage and reads back only the bundle's totals. */
    private fun syncMember(context: Context, host: String, prefs: Prefs): String? {
        report(context, host, prefs)?.let { return it }
        val plan = runCatching { get(host, "/v1/plan", prefs.laptopToken, 15_000, 0, prefs.memberId) }
            .getOrElse { return it.message ?: "could not reach the laptop" }
        prefs.memberPlan = plan.optJSONObject("plan")?.toString() ?: ""
        val db = Db.get(context)
        db.setMeta("laptop_last_sync", (System.currentTimeMillis() / 1000).toString())
        db.setMeta("laptop_device", "laptop")
        return null
    }

    /** The family's phones, as they reported to the laptop, for my phone's household view. */
    private fun pullPhones(context: Context, host: String, prefs: Prefs): String? {
        val db = Db.get(context)
        var since = ((db.meta("phones_synced_to")?.toLongOrNull() ?: 0L) - 86_400).coerceAtLeast(0)
        var json = runCatching { get(host, "/v1/phones", prefs.laptopToken, 30_000, since) }
            .getOrElse { return "could not read the family's phones: ${it.message}" }
        // A phone seen for the first time (it may have reported after my last look) is
        // taken from the start, not only from where the others left off.
        val known = (db.meta("phones_known") ?: "").split('\n').filter { it.isNotEmpty() }.toSet()
        val seen = json.optJSONArray("devices")?.let { a -> (0 until a.length()).map { a.getJSONObject(it).getString("name") } }.orEmpty()
        if (since > 0 && seen.any { it !in known }) {
            since = 0
            json = runCatching { get(host, "/v1/phones", prefs.laptopToken, 60_000, 0) }
                .getOrElse { return "could not read the family's phones: ${it.message}" }
        }
        val devices = json.optJSONArray("devices") ?: return null
        val w = db.writableDatabase
        w.beginTransaction()
        try {
            val names = mutableListOf<String>()
            for (i in 0 until devices.length()) {
                val d = devices.getJSONObject(i)
                val name = d.getString("name")
                names += name
                w.execSQL("DELETE FROM remote_usage WHERE device = ? AND hour >= ?", arrayOf(name, since))
                val hours = d.getJSONArray("hours")
                for (j in 0 until hours.length()) {
                    val r = hours.getJSONArray(j)
                    w.execSQL(
                        "INSERT OR REPLACE INTO remote_usage (device, hour, net, name, rx, tx) VALUES (?, ?, ?, ?, ?, ?)",
                        arrayOf(name, r.getLong(0), r.getString(1), r.getString(2), r.getLong(3), r.getLong(4))
                    )
                }
            }
            val now = System.currentTimeMillis() / 1000
            db.setMeta("phones_synced_to", (now - now % 3600).toString())
            db.setMeta("phones_known", (known + names).joinToString("\n"))
            w.setTransactionSuccessful()
        } finally {
            w.endTransaction()
        }
        return null
    }

    /**
     * Sends this phone's usage per network, and the plan with the ISP reading, so the laptop's
     * dashboard can split the plan into laptop, phone and family as well.
     */
    private fun report(context: Context, host: String, prefs: Prefs): String? {
        val db = Db.get(context)
        // The whole current cycle is sent every time, so the laptop's copy can never drift from
        // this phone's own (e.g. after hours are re-read more finely); two months the first time.
        val cycleStart = Period.cycleOf(java.time.LocalDate.now(), prefs.cycleDay).startEpoch
        val since = if (db.meta("phone_pushed_to") == null)
            Period.cycleOf(java.time.LocalDate.now().minusMonths(2), prefs.cycleDay).startEpoch else cycleStart
        val hours = org.json.JSONArray()
        db.readableDatabase.rawQuery(
            """SELECT u.hour, u.net, COALESCE(n.grp, u.name), SUM(u.rx), SUM(u.tx)
               FROM usage u LEFT JOIN networks n ON n.key = u.name
               WHERE u.hour >= ? GROUP BY 1, 2, 3""", arrayOf(since.toString())
        ).use { c ->
            while (c.moveToNext()) hours.put(org.json.JSONArray().put(c.getLong(0)).put(c.getString(1))
                .put(c.getString(2)).put(c.getLong(3)).put(c.getLong(4)))
        }
        val plan = JSONObject()
            .put("plan_gb", prefs.planGb).put("isp_used_gb", prefs.ispUsedGb).put("isp_at", prefs.ispAt)
            .put("cycle_day", prefs.cycleDay)
            .put("extra_gb", prefs.extraGb).put("extra_cycle", prefs.extraCycle)
            .put("plan_key", (prefs.planScope as? Scope.Named)?.key ?: "")
        val body = JSONObject().put("device", android.os.Build.MODEL).put("hours", hours).put("plan", plan)
            .toString().toByteArray()
        return runCatching {
            val nonce = nonce()
            val ts = System.currentTimeMillis() / 1000
            val digest = MessageDigest.getInstance("SHA-256").digest(body).joinToString("") { "%02x".format(it) }
            val sig = sign(prefs.laptopToken, "/v1/phone", digest, ts, nonce)
            val dev = if (prefs.isMember) "&dev=${prefs.memberId}" else ""
            val c = URL("http://$host:$PORT/v1/phone?ts=$ts&nonce=$nonce&sig=$sig$dev").openConnection() as HttpURLConnection
            c.connectTimeout = 3000
            c.readTimeout = 30_000
            c.requestMethod = "POST"
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            try {
                c.outputStream.use { it.write(body) }
                if (c.responseCode != 200) throw IllegalStateException("laptop said ${c.responseCode}")
                val reply = c.inputStream.bufferedReader().readText()
                if (!MessageDigest.isEqual((c.getHeaderField("X-Netusage-Sig") ?: "").toByteArray(),
                        sign(prefs.laptopToken, nonce, reply).toByteArray())) {
                    throw IllegalStateException("reply was not signed by the laptop")
                }
            } finally {
                c.disconnect()
            }
            val now = System.currentTimeMillis() / 1000
            db.setMeta("phone_pushed_to", (now - now % 3600).toString())
        }.exceptionOrNull()?.let { "could not send this phone's usage: ${it.message}" }
    }

    /** Which Wi-Fi network this phone used most in each hour and each day. */
    private class PhoneWifi(db: Db, since: Long) {
        private val zone = java.time.ZoneId.systemDefault()
        private val byHour = HashMap<Long, Pair<String, Long>>()
        private val byDay = HashMap<String, HashMap<String, Long>>()

        init {
            db.readableDatabase.rawQuery(
                """SELECT u.hour, COALESCE(n.grp, u.name), SUM(u.rx + u.tx) FROM usage u
                   LEFT JOIN networks n ON n.key = u.name
                   WHERE u.net = 'wifi' AND u.name != '' AND u.hour >= ? GROUP BY 1, 2""",
                arrayOf((since - 86_400).coerceAtLeast(0).toString())
            ).use { c ->
                while (c.moveToNext()) {
                    val hour = c.getLong(0)
                    val key = c.getString(1)
                    val v = c.getLong(2)
                    if (v > (byHour[hour]?.second ?: -1)) byHour[hour] = key to v
                    val day = day(hour)
                    val m = byDay.getOrPut(day) { HashMap() }
                    m[key] = (m[key] ?: 0) + v
                }
            }
        }

        private fun day(epoch: Long) = java.time.Instant.ofEpochSecond(epoch).atZone(zone).toLocalDate().toString()

        fun at(hour: Long): String =
            byHour[hour]?.first ?: byDay[day(hour)]?.maxByOrNull { it.value }?.key ?: ""
    }

    private fun nonce() = ByteArray(16).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }

    private fun sign(token: String, vararg parts: Any): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(token.toByteArray(), "HmacSHA256"))
        return mac.doFinal(parts.joinToString("\n").toByteArray()).joinToString("") { "%02x".format(it) }
    }

    private fun get(host: String, path: String, token: String, timeout: Int, since: Long = 0, dev: String = ""): JSONObject {
        val nonce = nonce()
        val ts = System.currentTimeMillis() / 1000
        val sig = sign(token, path, since, ts, nonce)
        val devParam = if (dev.isNotEmpty()) "&dev=$dev" else ""
        val c = URL("http://$host:$PORT$path?since=$since&ts=$ts&nonce=$nonce&sig=$sig$devParam")
            .openConnection() as HttpURLConnection
        c.connectTimeout = minOf(timeout, 3000)
        c.readTimeout = timeout
        try {
            if (c.responseCode != 200) throw IllegalStateException("laptop said ${c.responseCode}")
            val body = c.inputStream.bufferedReader().readText()
            val theirs = c.getHeaderField("X-Netusage-Sig") ?: ""
            if (!MessageDigest.isEqual(theirs.toByteArray(), sign(token, nonce, body).toByteArray())) {
                throw IllegalStateException("reply was not signed by the laptop")
            }
            return JSONObject(body)
        } finally {
            c.disconnect()
        }
    }

    private fun ping(host: String, token: String, dev: String = ""): String? =
        runCatching { get(host, "/v1/ping", token, 1500, 0, dev).optString("device") }.getOrNull()

    /** The laptop's address changed: ask every address on this Wi-Fi's /24 for it. */
    private fun find(context: Context, token: String, dev: String = ""): String? {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        val own = cm.getLinkProperties(cm.activeNetwork)?.linkAddresses
            ?.map { it.address }?.firstOrNull { it is Inet4Address && it.isSiteLocalAddress }
            ?: return null
        val prefix = own.hostAddress!!.substringBeforeLast('.')
        val pool = Executors.newFixedThreadPool(32)
        try {
            val tasks = (1..254).map { i ->
                Callable { "$prefix.$i".takeIf { ping(it, token, dev) != null } }
            }
            return pool.invokeAll(tasks, 20, TimeUnit.SECONDS)
                .firstNotNullOfOrNull { f -> runCatching { f.get() }.getOrNull() }
        } finally {
            pool.shutdownNow()
        }
    }
}
