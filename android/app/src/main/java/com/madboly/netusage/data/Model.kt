package com.madboly.netusage.data

import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

const val MOBILE = "mobile"
const val WIFI = "wifi"
const val WIRED = "wired"

/**
 * Which traffic a screen shows: everything, one network type, or one network group
 * (a router with all its names and bands, or a SIM). [key] is the group key.
 */
sealed class Scope {
    abstract val net: String?

    data object All : Scope() {
        override val net: String? get() = null
    }
    data class Type(override val net: String) : Scope()
    /** [title] is only for display; two scopes for the same network are equal. */
    class Named(override val net: String, val key: String, val title: String = "") : Scope() {
        override fun equals(other: Any?) = other is Named && other.net == net && other.key == key
        override fun hashCode() = net.hashCode() * 31 + key.hashCode()
        override fun toString() = "Named($net, $key)"
    }

    val label: String
        get() = when (this) {
            All -> "All networks"
            is Type -> when (net) {
                WIFI -> "All Wi-Fi"
                WIRED -> "All wired"
                else -> "All mobile"
            }
            is Named -> title.ifEmpty { networkLabel(net, key) }
        }

    /** Stored in preferences as "all", "wifi", "mobile" or "<net>:<group key>". */
    fun encode(): String = when (this) {
        All -> "all"
        is Type -> net
        is Named -> "$net:$key"
    }

    companion object {
        fun decode(s: String?): Scope = when {
            s == null || s == "all" -> All
            s == WIFI || s == MOBILE || s == WIRED -> Type(s)
            ':' in s -> Named(s.substringBefore(':'), s.substringAfter(':'))
            else -> All
        }
    }
}

fun networkLabel(net: String, key: String): String = when {
    key.isNotEmpty() -> key.substringAfter(':')
    net == WIFI -> "Unknown Wi-Fi"
    net == WIRED -> "Unknown wired"
    else -> "Unknown SIM"
}

enum class PeriodKind(val label: String) { DAY("Day"), WEEK("Week"), MONTH("Month"), CYCLE("Cycle"), ALL("All") }

/** Inclusive range of days. Week is the seven days ending on the chosen day. */
data class Period(val kind: PeriodKind, val start: LocalDate, val end: LocalDate) {
    val days: Long get() = end.toEpochDay() - start.toEpochDay() + 1
    fun contains(day: LocalDate) = !day.isBefore(start) && !day.isAfter(end)

    val label: String
        get() {
            val today = LocalDate.now()
            val dm = DateTimeFormatter.ofPattern("d MMM", Locale.getDefault())
            val dmy = DateTimeFormatter.ofPattern("d MMM yyyy", Locale.getDefault())
            return when (kind) {
                PeriodKind.DAY -> when (start) {
                    today -> "Today"
                    today.minusDays(1) -> "Yesterday"
                    else -> start.format(DateTimeFormatter.ofPattern("EEE d MMM yyyy", Locale.getDefault()))
                }
                PeriodKind.MONTH -> start.format(DateTimeFormatter.ofPattern("MMMM yyyy", Locale.getDefault()))
                PeriodKind.ALL -> "All time"
                else -> "${start.format(dm)} – ${end.format(dmy)}"
            }
        }

    val startEpoch: Long get() = start.atStartOfDay(ZoneId.systemDefault()).toEpochSecond()
    val endEpoch: Long get() = end.plusDays(1).atStartOfDay(ZoneId.systemDefault()).toEpochSecond()

    companion object {
        fun cycleOf(day: LocalDate, cycleDay: Int): Period {
            fun anchor(month: LocalDate) = month.withDayOfMonth(minOf(cycleDay, month.lengthOfMonth()))
            var start = anchor(day)
            if (start.isAfter(day)) start = anchor(day.minusMonths(1))
            val end = anchor(start.plusMonths(1)).minusDays(1)
            return Period(PeriodKind.CYCLE, start, end)
        }

        fun of(kind: PeriodKind, anchor: LocalDate, cycleDay: Int, firstDay: LocalDate): Period = when (kind) {
            PeriodKind.DAY -> Period(kind, anchor, anchor)
            PeriodKind.WEEK -> Period(kind, anchor.minusDays(6), anchor)
            PeriodKind.MONTH -> Period(kind, anchor.withDayOfMonth(1), anchor.withDayOfMonth(anchor.lengthOfMonth()))
            PeriodKind.CYCLE -> cycleOf(anchor, cycleDay)
            PeriodKind.ALL -> Period(kind, minOf(firstDay, anchor), LocalDate.now())
        }
    }
}

/** Anchor day after stepping a period back (-1) or forward (+1). */
fun step(period: Period, steps: Int): LocalDate = when (period.kind) {
    PeriodKind.DAY -> period.start.plusDays(steps.toLong())
    PeriodKind.WEEK -> period.end.plusDays(7L * steps)
    PeriodKind.MONTH -> period.start.plusMonths(steps.toLong())
    PeriodKind.CYCLE -> if (steps < 0) period.start.minusDays(1) else period.end.plusDays(1)
    PeriodKind.ALL -> period.end
}

private val UNITS = listOf("TB" to (1L shl 40), "GB" to (1L shl 30), "MB" to (1L shl 20), "KB" to (1L shl 10))

/** Bytes in the same 1024-based units the laptop app and My Data Manager use. */
fun human(bytes: Long, precision: Int = 2): String {
    for ((unit, size) in UNITS) {
        if (bytes >= size) {
            val v = bytes.toDouble() / size
            val p = if (v >= 100) 0 else if (v >= 10) minOf(precision, 1) else precision
            return "%.${p}f %s".format(Locale.US, v, unit)
        }
    }
    return "$bytes B"
}

fun gbToBytes(gb: Double): Long = (gb * (1L shl 30)).toLong()
