package com.madboly.netusage.data

import android.content.Context
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.YearMonth

const val PHONE = "~phone"

data class Totals(val rx: Long = 0, val tx: Long = 0, val unsplit: Long = 0) {
    val total: Long get() = rx + tx + unsplit
    operator fun plus(o: Totals) = Totals(rx + o.rx, tx + o.tx, unsplit + o.unsplit)
}

/** [parts] is the same total split by device, when a chart stacks by device. */
data class Bucket(
    val label: String, val start: LocalDate, val totals: Totals, val current: Boolean,
    val parts: List<Long> = emptyList(),
)

data class NetworkRow(
    val net: String, val key: String, val label: String, val totals: Totals,
    val keys: List<String>, val names: List<String>, val lastSeen: Long,
)

data class AppRow(val app: String, val totals: Totals)

/** One network key as stored, with the group it is shown under. */
data class NetworkKey(val key: String, val net: String, val label: String, val grp: String, val names: List<String>)

data class DeviceRow(val device: String, val totals: Totals)

/**
 * [devicesMode] says what other devices add to this phone's own traffic: "all" (the
 * laptop and the family's phones), "mine" (the laptop) or "phone" (nothing).
 */
class Repo(context: Context, var devicesMode: String = "phone") {
    val withDevices: Boolean get() = devicesMode != "phone"
    private val db = Db.get(context)
    private val zone: ZoneId get() = ZoneId.systemDefault()

    // ---- network identity ----

    /**
     * Records that [key] was seen with [name]. The first time a router appears, history
     * imported under the same SSID is folded into it, so it continues after a rename.
     */
    fun seeNetwork(key: String, net: String, name: String, now: Long) {
        val w = db.writableDatabase
        val existing = network(key)
        if (existing == null) {
            w.execSQL(
                "INSERT INTO networks (key, net, label, grp, names, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                arrayOf(key, net, name, key, name, now)
            )
        } else {
            val names = if (name.isEmpty() || name in existing.names) existing.names else existing.names + name
            w.execSQL(
                "UPDATE networks SET label = ?, names = ?, last_seen = ? WHERE key = ?",
                arrayOf(name.ifEmpty { existing.label }, names.joinToString("\n"), now, key)
            )
        }
        adoptImported(key, name)
    }

    /**
     * Folds history imported under an SSID into the router now broadcasting it. Done once
     * per SSID, so separating them later in the Networks tab sticks.
     */
    private fun adoptImported(key: String, name: String) {
        if (!key.startsWith("ap:") || name.isEmpty()) return
        val old = network("ssid:$name") ?: return
        val flag = "adopted:${old.key}"
        if (db.meta(flag) != null) return
        if (old.grp == old.key) merge(old.key, key)
        db.setMeta(flag, key)
    }

    fun network(key: String): NetworkKey? =
        db.readableDatabase.rawQuery(
            "SELECT key, net, label, grp, names FROM networks WHERE key = ?", arrayOf(key)
        ).use { c ->
            if (!c.moveToFirst()) null
            else NetworkKey(c.getString(0), c.getString(1), c.getString(2), c.getString(3),
                c.getString(4).split('\n').filter { it.isNotEmpty() })
        }

    /** Show everything in [key]'s group under [into]'s group. */
    fun merge(key: String, into: String) {
        val from = network(key)?.grp ?: return
        val to = network(into)?.grp ?: return
        if (from == to) return
        db.writableDatabase.execSQL("UPDATE networks SET grp = ? WHERE grp = ?", arrayOf(to, from))
    }

    fun unmerge(key: String) {
        db.writableDatabase.execSQL("UPDATE networks SET grp = key WHERE key = ?", arrayOf(key))
        // Keys that pointed at this one as their group stay with it.
    }

    fun setCustomLabel(grp: String, label: String) =
        db.setMeta("label:$grp", label.trim())

    private fun customLabel(grp: String): String? = db.meta("label:$grp")?.takeIf { it.isNotEmpty() }

    // ---- usage queries ----

    private class Row(val hour: Long, val net: String, val grp: String, val rx: Long, val tx: Long, val split: Boolean)

    private fun where(scope: Scope, t: String): Pair<String, Array<String>> = when (scope) {
        Scope.All -> "" to emptyArray()
        is Scope.Type -> " AND $t.net = ?" to arrayOf(scope.net)
        is Scope.Named -> " AND $t.net = ? AND COALESCE(n.grp, $t.name) = ?" to arrayOf(scope.net, scope.key)
    }

    private fun rows(scope: Scope, from: Long, to: Long, device: String? = null): List<Row> {
        val range = arrayOf(from.toString(), to.toString())
        val (uc, ua) = where(scope, "u")
        val (rc, ra) = where(scope, "r")
        val local = """SELECT u.hour, u.net, COALESCE(n.grp, u.name), u.rx, u.tx, u.split
               FROM usage u LEFT JOIN networks n ON n.key = u.name
               WHERE u.hour >= ? AND u.hour < ?$uc"""
        val remote = """SELECT r.hour, r.net, COALESCE(n.grp, r.name), r.rx, r.tx, 1
               FROM remote_usage r LEFT JOIN networks n ON n.key = r.name
               WHERE r.hour >= ? AND r.hour < ?$rc"""
        val (sql, args) = when {
            device == PHONE -> local to arrayOf(*range, *ua)
            device != null -> "$remote AND r.device = ?" to arrayOf(*range, *ra, device)
            devicesMode == "mine" -> "$local UNION ALL $remote AND r.device = ?" to
                arrayOf(*range, *ua, *range, *ra, laptopName() ?: "")
            withDevices -> "$local UNION ALL $remote" to arrayOf(*range, *ua, *range, *ra)
            else -> local to arrayOf(*range, *ua)
        }
        return db.readableDatabase.rawQuery(sql, args).use { c ->
            buildList {
                while (c.moveToNext()) add(Row(c.getLong(0), c.getString(1), c.getString(2),
                    c.getLong(3), c.getLong(4), c.getInt(5) == 1))
            }
        }
    }

    /** This phone and each other device, for the period and scope. */
    fun devices(scope: Scope, period: Period): List<DeviceRow> {
        return (listOf(PHONE) + otherDevices()).map { d ->
            DeviceRow(d, rows(scope, period.startEpoch, period.endEpoch, d).fold(Totals()) { a, r -> a + r.totals() })
        }
    }

    private fun Row.totals() = if (split) Totals(rx, tx) else Totals(unsplit = rx + tx)
    private fun day(hour: Long): LocalDate = Instant.ofEpochSecond(hour).atZone(zone).toLocalDate()

    /** [device] limits to this phone ([PHONE]) or one other device; null follows [withDevices]. */
    fun totals(scope: Scope, period: Period, device: String? = null): Totals =
        rows(scope, period.startEpoch, period.endEpoch, device).fold(Totals()) { a, r -> a + r.totals() }

    /** Totals between two instants, for plan figures anchored to an ISP reading. */
    fun totalsBetween(scope: Scope, from: Long, to: Long, device: String? = null): Totals =
        rows(scope, from, to, device).fold(Totals()) { a, r -> a + r.totals() }

    fun daily(scope: Scope, from: LocalDate, to: LocalDate, device: String? = null): Map<LocalDate, Totals> {
        val p = Period(PeriodKind.MONTH, from, to)
        val out = sortedMapOf<LocalDate, Totals>()
        for (r in rows(scope, p.startEpoch, p.endEpoch, device)) {
            val d = day(r.hour)
            out[d] = (out[d] ?: Totals()) + r.totals()
        }
        return out
    }

    /** Chart buckets: hourly for a day, monthly for all time, daily otherwise. */
    /** The period's buckets with [Bucket.parts] filled per device: this phone first. */
    fun seriesByDevice(scope: Scope, period: Period): List<Bucket> {
        val devices = listOf(PHONE) + otherDevices()
        val each = devices.map { series(scope, period, it) }
        return each.first().mapIndexed { i, b ->
            val parts = each.map { it.getOrNull(i)?.totals?.total ?: 0L }
            b.copy(totals = each.fold(Totals()) { a, s -> a + (s.getOrNull(i)?.totals ?: Totals()) }, parts = parts)
        }
    }

    /** The laptop first, then the family's phones. */
    fun otherDevices(): List<String> {
        val all = db.readableDatabase.rawQuery("SELECT DISTINCT device FROM remote_usage", null)
            .use { c -> buildList { while (c.moveToNext()) add(c.getString(0)) } }
        val laptop = laptopName()
        val shown = if (devicesMode == "mine") all.filter { it == laptop } else all
        return shown.sortedBy { if (it == laptop) "" else it }
    }

    fun series(scope: Scope, period: Period, device: String? = null): List<Bucket> {
        val now = System.currentTimeMillis() / 1000
        val today = LocalDate.now()
        return when (period.kind) {
            PeriodKind.DAY -> {
                val sums = HashMap<Int, Totals>()
                for (r in rows(scope, period.startEpoch, period.endEpoch, device)) {
                    val h = Instant.ofEpochSecond(r.hour).atZone(zone).hour
                    sums[h] = (sums[h] ?: Totals()) + r.totals()
                }
                (0..23).map { h ->
                    val start = period.start.atTime(h, 0).atZone(zone).toEpochSecond()
                    Bucket("%02d".format(h), period.start, sums[h] ?: Totals(), now in start until start + 3600)
                }
            }
            PeriodKind.ALL -> {
                val d = daily(scope, period.start, period.end, device)
                val months = d.entries.groupBy { YearMonth.from(it.key) }
                var m = YearMonth.from(period.start)
                buildList {
                    while (!m.isAfter(YearMonth.from(period.end))) {
                        val t = months[m]?.fold(Totals()) { a, e -> a + e.value } ?: Totals()
                        add(Bucket(m.month.name.take(3).lowercase().replaceFirstChar { it.uppercase() },
                            m.atDay(1), t, m == YearMonth.from(today)))
                        m = m.plusMonths(1)
                    }
                }
            }
            else -> {
                val d = daily(scope, period.start, period.end, device)
                (0 until period.days).map {
                    val day = period.start.plusDays(it)
                    Bucket(day.dayOfMonth.toString(), day, d[day] ?: Totals(), day == today)
                }
            }
        }
    }

    fun networks(period: Period, net: String? = null): List<NetworkRow> {
        val sums = HashMap<Pair<String, String>, Totals>()
        val scope = if (net == null) Scope.All else Scope.Type(net)
        for (r in rows(scope, period.startEpoch, period.endEpoch)) {
            val k = r.net to r.grp
            sums[k] = (sums[k] ?: Totals()) + r.totals()
        }
        val keys = allKeys().groupBy { it.net to it.grp }
        val lastSeen = db.readableDatabase.rawQuery(
            "SELECT grp, MAX(last_seen) FROM networks GROUP BY grp", null
        ).use { c -> buildMap { while (c.moveToNext()) put(c.getString(0), c.getLong(1)) } }
        return sums.map { (k, t) ->
            val members = keys[k].orEmpty()
            val head = members.firstOrNull { it.key == k.second }
            NetworkRow(
                net = k.first, key = k.second,
                label = customLabel(k.second) ?: head?.label?.ifEmpty { null } ?: networkLabel(k.first, k.second),
                totals = t, keys = members.map { it.key },
                names = members.flatMap { it.names }.distinct(),
                lastSeen = lastSeen[k.second] ?: 0,
            )
        }.sortedByDescending { it.totals.total }
    }

    fun allKeys(): List<NetworkKey> =
        db.readableDatabase.rawQuery("SELECT key, net, label, grp, names FROM networks ORDER BY last_seen DESC", null).use { c ->
            buildList {
                while (c.moveToNext()) add(NetworkKey(c.getString(0), c.getString(1), c.getString(2),
                    c.getString(3), c.getString(4).split('\n').filter { it.isNotEmpty() }))
            }
        }

    fun labelFor(scope: Scope): String = when (scope) {
        is Scope.Named -> customLabel(scope.key) ?: network(scope.key)?.label?.ifEmpty { null } ?: scope.label
        else -> scope.label
    }

    fun firstDay(): LocalDate =
        db.readableDatabase.rawQuery(
            if (withDevices) "SELECT MIN(h) FROM (SELECT MIN(hour) h FROM usage UNION ALL SELECT MIN(hour) FROM remote_usage)"
            else "SELECT MIN(hour) FROM usage", null
        ).use { c ->
            if (c.moveToFirst() && !c.isNull(0)) day(c.getLong(0)) else LocalDate.now()
        }

    // ---- apps ----

    private fun appWhere(scope: Scope): Pair<String, Array<String>> =
        scope.net?.let { " AND net = ?" to arrayOf(it) } ?: ("" to emptyArray())

    fun apps(scope: Scope, period: Period): List<AppRow> {
        val (clause, args) = appWhere(scope)
        return db.readableDatabase.rawQuery(
            """SELECT app, SUM(rx), SUM(tx) FROM app_usage
               WHERE day >= ? AND day <= ?$clause GROUP BY app ORDER BY SUM(rx + tx) DESC""",
            arrayOf(period.start.toString(), period.end.toString(), *args)
        ).use { c ->
            buildList { while (c.moveToNext()) add(AppRow(c.getString(0), Totals(c.getLong(1), c.getLong(2)))) }
        }.filter { it.totals.total > 0 }
    }

    fun appDaily(app: String, scope: Scope, period: Period): Map<LocalDate, Totals> {
        val (clause, args) = appWhere(scope)
        return db.readableDatabase.rawQuery(
            """SELECT day, SUM(rx), SUM(tx) FROM app_usage
               WHERE app = ? AND day >= ? AND day <= ?$clause GROUP BY day""",
            arrayOf(app, period.start.toString(), period.end.toString(), *args)
        ).use { c ->
            buildMap { while (c.moveToNext()) put(LocalDate.parse(c.getString(0)), Totals(c.getLong(1), c.getLong(2))) }
        }
    }

    fun appFirstSeen(app: String): LocalDate? =
        db.readableDatabase.rawQuery("SELECT MIN(day) FROM app_usage WHERE app = ?", arrayOf(app)).use { c ->
            if (c.moveToFirst() && !c.isNull(0)) LocalDate.parse(c.getString(0)) else null
        }

    fun appDataStart(): LocalDate? =
        db.readableDatabase.rawQuery("SELECT MIN(day) FROM app_usage", null).use { c ->
            if (c.moveToFirst() && !c.isNull(0)) LocalDate.parse(c.getString(0)) else null
        }

    fun lastLaptopSync(): Long = db.meta("laptop_last_sync")?.toLongOrNull() ?: 0
    fun laptopName(): String? = db.meta("laptop_device")
    fun laptopError(): String = db.meta("laptop_error") ?: ""
    fun lastSync(): Long = db.meta("last_sync")?.toLongOrNull() ?: 0
    fun lastSample(): Long = db.meta("last_sample")?.toLongOrNull() ?: 0
}
