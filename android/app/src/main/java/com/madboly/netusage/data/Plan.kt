package com.madboly.netusage.data

import java.time.LocalDate
import java.time.temporal.ChronoUnit

/**
 * Where the data plan stands. The ISP's own figure is the truth, since it also sees
 * every other device on the router; when one has been entered for this cycle, usage is
 * counted from it onwards, and whatever the ISP counted that this phone and the laptop
 * did not see is shown as other devices.
 */
data class PlanStatus(
    val cycle: Period,
    val plan: Long,
    val used: Long,
    val byDevice: List<Pair<String, Long>>,   // PHONE, each other device, then family
    val reportedAt: Long?,                    // when the ISP figure was entered, if this cycle
) {
    val remaining: Long get() = plan - used
    val daysLeft: Long get() = ChronoUnit.DAYS.between(LocalDate.now(), cycle.end) + 1
    val elapsedDays: Long get() = ChronoUnit.DAYS.between(cycle.start, LocalDate.now()) + 1
    val perDayLeft: Long get() = if (remaining > 0) remaining / daysLeft.coerceAtLeast(1) else 0
    val projected: Long get() = used * cycle.days / elapsedDays.coerceAtLeast(1)
    val fraction: Float get() = if (plan > 0) used.toFloat() / plan else 0f
}

/** Everything the ISP counted that neither this phone nor the laptop saw: the family. */
const val OTHER_DEVICES = "~other"

/** [household] must show every device (devicesMode "all"). */
fun planStatus(prefs: Prefs, household: Repo): PlanStatus? {
    if (prefs.planGb <= 0) return null
    val cycle = Period.cycleOf(LocalDate.now(), prefs.cycleDay)
    val scope = prefs.planScope
    val devices = listOf(PHONE) + household.otherDevices()
    // The laptop's Wi-Fi from before it tracked networks has no router attached. It lives
    // on the home Wi-Fi, so on a Wi-Fi plan that untagged traffic counts toward it too.
    val untagged = if (scope is Scope.Named && scope.net == WIFI) Scope.Named(WIFI, "") else null
    fun deviceTotal(device: String, from: Long, to: Long): Long {
        val own = household.totalsBetween(scope, from, to, device).total
        return own + if (untagged != null && device != PHONE)
            household.totalsBetween(untagged, from, to, device).total else 0L
    }
    val tracked = devices.map { it to deviceTotal(it, cycle.startEpoch, cycle.endEpoch) }
    val trackedSum = tracked.sumOf { it.second }
    val at = prefs.ispAt.takeIf { it in cycle.startEpoch until cycle.endEpoch }
    val other = if (at != null) {
        val before = devices.sumOf { deviceTotal(it, cycle.startEpoch, at) }
        (gbToBytes(prefs.ispUsedGb) - before).coerceAtLeast(0)
    } else 0L
    return PlanStatus(
        cycle = cycle,
        // an extra bundle only counts in the cycle it was bought for
        plan = gbToBytes(prefs.planGb + if (prefs.extraCycle == cycle.start.toString()) prefs.extraGb else 0.0),
        used = trackedSum + other,
        byDevice = tracked + (OTHER_DEVICES to other),
        reportedAt = at,
    )
}
