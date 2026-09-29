package com.madboly.netusage.data

import android.app.AppOpsManager
import android.app.usage.NetworkStats
import android.app.usage.NetworkStatsManager
import android.content.Context
import android.net.ConnectivityManager
import android.os.Build
import android.os.Process
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

object Sync {
    private const val RETENTION_DAYS = 100L

    fun hasUsageAccess(context: Context): Boolean {
        val ops = context.getSystemService(AppOpsManager::class.java)
        val mode = if (Build.VERSION.SDK_INT >= 29)
            ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        else @Suppress("DEPRECATION")
            ops.checkOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        return mode == AppOpsManager.MODE_ALLOWED
    }

    /**
     * Copies Android's network history into the local store: hourly totals per network
     * type, shared out between routers and SIMs by the tracker's samples, and daily totals
     * per app. Re-reads from the start of the day before the last sync, so days are always
     * written whole.
     */
    @Synchronized
    fun run(context: Context) {
        importOnce(context)
        Prefs(context).applyBundled(context)
        if (!hasUsageAccess(context)) return
        val db = Db.get(context)
        val zone = ZoneId.systemDefault()
        val now = System.currentTimeMillis()
        // Per-app keys changed (Dual Apps copies and system uids merged): read everything again once.
        val appsVersion = "2"
        val synced = db.meta("synced_to")?.toLongOrNull()?.takeIf { db.meta("apps_version") == appsVersion }
        val fromDay = if (synced == null) LocalDate.now().minusDays(RETENTION_DAYS)
        else Instant.ofEpochSecond(synced).atZone(zone).toLocalDate().minusDays(1)
        val from = fromDay.atStartOfDay(zone).toEpochSecond()

        // Before this moment the imported history is complete; Android's own leftovers
        // from the same days would count twice.
        val importedUntil = db.meta("import_until")?.toLongOrNull() ?: 0
        val importedUntilDay = Instant.ofEpochSecond(importedUntil).atZone(zone).toLocalDate().toString()

        val nsm = context.getSystemService(NetworkStatsManager::class.java)
        val apps = AppNames(context)
        val w = db.writableDatabase
        w.beginTransaction()
        try {
            for ((type, net) in listOf(ConnectivityManager.TYPE_WIFI to WIFI, ConnectivityManager.TYPE_MOBILE to MOBILE)) {
                val hours = HashMap<Long, LongArray>()
                val appDays = HashMap<Pair<String, String>, LongArray>()
                val b = NetworkStats.Bucket()
                for (subscriber in subscribers(context, type)) {
                    val stats = runCatching { nsm.queryDetails(type, subscriber, from * 1000, now) }.getOrNull() ?: continue
                    stats.use {
                    while (it.hasNextBucket()) {
                        it.getNextBucket(b)
                        val start = b.startTimeStamp / 1000
                        val hour = start - start % 3600
                        hours.getOrPut(hour) { LongArray(2) }.let { a -> a[0] += b.rxBytes; a[1] += b.txBytes }
                        val day = Instant.ofEpochSecond(start).atZone(zone).toLocalDate().toString()
                        appDays.getOrPut(day to apps.key(b.uid)) { LongArray(2) }
                            .let { a -> a[0] += b.rxBytes; a[1] += b.txBytes }
                    }
                    }
                }

                // Per-app history comes in 2-hour blocks and leaves out the block in
                // progress. For recent days, device totals are read hour by hour instead,
                // which also covers the current hour. The cut is on a 2-hour boundary so no
                // block is counted twice.
                val nowSec = now / 1000
                val recent = maxOf(from, nowSec - 3 * 86_400).let { it - it % 7200 }
                hours.keys.removeAll { it >= recent }
                var h = recent
                while (h <= nowSec) {
                    // zero hours are kept so an older 2-hour row for them is cleared
                    val sum = LongArray(2)
                    var got = false
                    for (subscriber in subscribers(context, type)) {
                        val b2 = runCatching { nsm.querySummaryForDevice(type, subscriber, h * 1000, (h + 3600) * 1000) }.getOrNull() ?: continue
                        sum[0] += b2.rxBytes
                        sum[1] += b2.txBytes
                        got = true
                    }
                    if (got) hours[h] = sum
                    h += 3600
                }

                val imported = db.importedHours(net, from)
                val fallback = db.meta("cur:$net") ?: ""
                for ((hour, v) in hours) {
                    if (hour in imported || hour < importedUntil) continue
                    w.execSQL("DELETE FROM usage WHERE hour = ? AND net = ? AND src = 'sync'", arrayOf(hour, net))
                    if (v[0] + v[1] == 0L) continue
                    for ((key, share) in split(db.samples(hour, net), fallback, v[0], v[1])) {
                        w.execSQL(
                            "INSERT OR REPLACE INTO usage (hour, net, name, rx, tx, split, src) VALUES (?, ?, ?, ?, ?, 1, 'sync')",
                            arrayOf(hour, net, key, share[0], share[1])
                        )
                    }
                }

                appDays.keys.removeAll { it.first < importedUntilDay }
                val days = appDays.keys.map { it.first }.toSet()
                for (day in days) w.execSQL("DELETE FROM app_usage WHERE day = ? AND net = ?", arrayOf(day, net))
                for ((k, v) in appDays) {
                    if (v[0] + v[1] == 0L) continue
                    w.execSQL(
                        "INSERT INTO app_usage (day, net, app, rx, tx) VALUES (?, ?, ?, ?, ?)",
                        arrayOf(k.first, net, k.second, v[0], v[1])
                    )
                }
            }
            val nowSec = now / 1000
            db.setMeta("synced_to", (nowSec - nowSec % 3600).toString())
            db.setMeta("apps_version", appsVersion)
            db.setMeta("last_sync", nowSec.toString())
            w.setTransactionSuccessful()
        } finally {
            w.endTransaction()
        }
        if (Laptop.configured(Prefs(context))) {
            db.setMeta("laptop_error", Laptop.sync(context) ?: "")
        }
        Alerts.check(context)
    }

    /**
     * Who to ask about for a network type. Android 10+ takes null for "every SIM"; Android 9
     * needs each SIM's subscriber id, which READ_PHONE_STATE still allows there.
     */
    @Suppress("DEPRECATION")
    @android.annotation.SuppressLint("MissingPermission", "HardwareIds")
    private fun subscribers(context: Context, type: Int): List<String?> {
        if (type != ConnectivityManager.TYPE_MOBILE || Build.VERSION.SDK_INT >= 29) return listOf(null)
        return runCatching {
            val sm = context.getSystemService(android.telephony.SubscriptionManager::class.java)
            val tm = context.getSystemService(android.telephony.TelephonyManager::class.java)
            sm.activeSubscriptionInfoList.orEmpty()
                .mapNotNull { tm.createForSubscriptionId(it.subscriptionId).subscriberId }
                .distinct()
        }.getOrNull()?.ifEmpty { null } ?: listOf(null)
    }

    /** Shares one hour's rx/tx between networks in proportion to the tracker's samples. */
    private fun split(samples: Map<String, Long>, fallback: String, rx: Long, tx: Long): Map<String, LongArray> {
        val weights = samples.filterValues { it > 0 }
        if (weights.isEmpty()) return mapOf(fallback to longArrayOf(rx, tx))
        val sum = weights.values.sum().toDouble()
        val out = LinkedHashMap<String, LongArray>()
        var rxLeft = rx
        var txLeft = tx
        val entries = weights.entries.sortedBy { it.value }
        entries.forEachIndexed { i, (key, w) ->
            if (i == entries.lastIndex) out[key] = longArrayOf(rxLeft, txLeft)
            else {
                val r = (rx * w / sum).toLong()
                val t = (tx * w / sum).toLong()
                out[key] = longArrayOf(r, t)
                rxLeft -= r
                txLeft -= t
            }
        }
        return out
    }

    /**
     * Loads assets/import.json on first run: My Data Manager's daily totals from before
     * Android's own history starts, plus the per-network hourly history read from the
     * phone over adb, which is the only place old Wi-Fi names survive.
     */
    fun importOnce(context: Context) {
        val db = Db.get(context)
        if (db.meta("imported") == "1") return
        val text = runCatching { context.assets.open("import.json").bufferedReader().readText() }.getOrNull()
        if (text == null) {
            db.setMeta("imported", "1")
            return
        }
        val json = JSONObject(text)
        val w = db.writableDatabase
        w.beginTransaction()
        try {
            val nets = json.getJSONArray("networks")
            for (i in 0 until nets.length()) {
                val n = nets.getJSONObject(i)
                w.execSQL(
                    "INSERT OR IGNORE INTO networks (key, net, label, grp, names, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                    arrayOf(n.getString("key"), n.getString("net"), n.getString("label"), n.getString("key"),
                        n.getString("label"), n.optLong("last_seen"))
                )
            }
            val rows = json.getJSONArray("usage")
            for (i in 0 until rows.length()) {
                val r = rows.getJSONArray(i)
                w.execSQL(
                    "INSERT OR REPLACE INTO usage (hour, net, name, rx, tx, split, src) VALUES (?, ?, ?, ?, ?, ?, 'import')",
                    arrayOf(r.getLong(0), r.getString(1), r.getString(2), r.getLong(3), r.getLong(4), r.getInt(5))
                )
            }
            // My Data Manager only showed app names; match them to installed packages.
            json.optJSONArray("apps")?.let { apps ->
                val byLabel = HashMap<String, String>()
                val pm = context.packageManager
                for (info in pm.getInstalledApplications(0)) {
                    byLabel.putIfAbsent(pm.getApplicationLabel(info).toString().lowercase(), info.packageName)
                }
                for (i in 0 until apps.length()) {
                    val r = apps.getJSONArray(i)
                    val name = r.getString(2)
                    val key = byLabel[name.lowercase()] ?: "label:$name"
                    // add rather than replace (see Db.addSample for why not an upsert)
                    w.execSQL("INSERT OR IGNORE INTO app_usage (day, net, app, rx, tx) VALUES (?, ?, ?, 0, 0)",
                        arrayOf(r.getString(0), r.getString(1), key))
                    w.execSQL("UPDATE app_usage SET rx = rx + ? WHERE day = ? AND net = ? AND app = ?",
                        arrayOf(r.getLong(3), r.getString(0), r.getString(1), key))
                }
            }
            json.optJSONObject("settings")?.let { Prefs(context).applyImported(it) }
            db.setMeta("import_until", json.optLong("until").toString())
            db.setMeta("imported", "1")
            db.setMeta("import_summary", json.optString("summary"))
            w.setTransactionSuccessful()
        } finally {
            w.endTransaction()
        }
    }
}

/** Stable keys for the uids in Android's history, and friendly names for them. */
class AppNames(private val context: Context) {
    private val pm = context.packageManager
    private val cache = HashMap<Int, String>()

    /**
     * An app is one key however many copies it has: a Dual Apps copy runs under another
     * user (uid 95xxxxx) but is the same app, as My Data Manager counts it. Every uid
     * below the app range belongs to the system.
     */
    fun key(uid: Int): String = cache.getOrPut(uid) {
        val appId = uid % 100_000
        when {
            uid == NetworkStats.Bucket.UID_REMOVED -> "~removed"
            uid == NetworkStats.Bucket.UID_TETHERING -> "~tethering"
            uid < 0 || appId < Process.FIRST_APPLICATION_UID -> "~system"
            else -> (pm.getPackagesForUid(uid) ?: pm.getPackagesForUid(appId))?.minOrNull() ?: "~uid:$appId"
        }
    }

    companion object {
        fun label(context: Context, key: String): String = when {
            key == "~removed" -> "Removed or reinstalled apps"
            key == "~tethering" -> "Hotspot & tethering"
            key == "~system" -> "OS services"
            key.startsWith("~uid:") -> "System service ${key.removePrefix("~uid:")}"
            key.startsWith("label:") -> key.removePrefix("label:")
            else -> runCatching {
                val pm = context.packageManager
                pm.getApplicationLabel(pm.getApplicationInfo(key, 0)).toString()
            }.getOrDefault(key.substringAfterLast('.'))
        }

        fun icon(context: Context, key: String) = if (key.startsWith("~") || key.startsWith("label:")) null
        else runCatching { context.packageManager.getApplicationIcon(key) }.getOrNull()

        fun exists(context: Context, key: String) = key.startsWith("~") ||
            runCatching { context.packageManager.getApplicationInfo(key, 0); true }.getOrDefault(false)
    }
}
