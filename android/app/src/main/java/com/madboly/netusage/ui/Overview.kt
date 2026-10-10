package com.madboly.netusage.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.madboly.netusage.data.AppNames
import com.madboly.netusage.data.AppRow
import com.madboly.netusage.data.Bucket
import com.madboly.netusage.data.DeviceRow
import com.madboly.netusage.data.MOBILE
import com.madboly.netusage.data.NetworkRow
import com.madboly.netusage.data.OTHER_DEVICES
import com.madboly.netusage.data.PHONE
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.PlanStatus
import com.madboly.netusage.data.Scope
import com.madboly.netusage.data.Totals
import com.madboly.netusage.data.gbToBytes
import com.madboly.netusage.data.human
import com.madboly.netusage.data.planStatus
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

/** Colours for this phone, the laptop (and any further device), then the family. */
private val DEVICE_COLORS = listOf(Teal, Mint, Color(0xFF5C8DF6), Color(0xFFE66BA8))
val FamilyColor = WifiColor

/** The laptop's device name, set once the laptop has been reached; family phones go by their own. */
var laptopDevice: String? = null

fun deviceName(device: String) = when (device) {
    PHONE -> "This phone"
    OTHER_DEVICES -> "Others"
    laptopDevice -> "Laptop"
    else -> device
}

private fun deviceColor(index: Int, device: String) =
    if (device == OTHER_DEVICES) FamilyColor else DEVICE_COLORS[index % DEVICE_COLORS.size]

private class OverviewData(
    val totals: Totals = Totals(),
    val buckets: List<Bucket> = emptyList(),
    val networks: List<NetworkRow> = emptyList(),
    val apps: List<AppRow> = emptyList(),
    val today: Long = 0,
    val plan: PlanStatus? = null,
    val dailyLabel: String = "",
    val planLabel: String = "",
    val devices: List<DeviceRow> = emptyList(),
)

@Composable
fun OverviewScreen(state: AppState, openNetworks: () -> Unit, openApps: () -> Unit) {
    val prefs = state.prefs
    val period = state.period
    var byDevice by rememberSaveable { mutableStateOf(true) }
    var editIsp by remember { mutableStateOf(false) }
    val d = load(OverviewData(), state.scope, period, state.version, state.devicesMode) {
        val repo = state.repo
        laptopDevice = repo.laptopName()
        val today = LocalDate.now()
        val devices = if (state.withDevices) repo.devices(state.scope, period) else emptyList()
        val split = devices.count { it.totals.total > 0 } > 1 || (state.withDevices && repo.otherDevices().isNotEmpty())
        OverviewData(
            totals = repo.totals(state.scope, period),
            buckets = if (split) repo.seriesByDevice(state.scope, period) else repo.series(state.scope, period),
            networks = repo.networks(period, state.scope.net).let { rows ->
                if (state.scope is Scope.Named) rows.filter { it.key == (state.scope as Scope.Named).key } else rows
            },
            apps = repo.apps(state.scope, period).take(6),
            // Limits and the plan cover the whole household, whatever this screen shows.
            today = state.household.totals(prefs.dailyScope, Period(PeriodKind.DAY, today, today)).total,
            plan = if (prefs.isMember) null else planStatus(prefs, state.household),
            devices = devices,
            dailyLabel = repo.labelFor(prefs.dailyScope),
            planLabel = repo.labelFor(prefs.planScope),
        )
    }
    val showDevices = d.buckets.any { it.parts.size > 1 }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        if (prefs.isMember) MemberPlan(prefs.memberPlan, state.version)
        else d.plan?.let { PlanHero(it, d.planLabel, onUpdate = { editIsp = true }) }

        PeriodBar(state)
        TotalPanel(d.totals, period, d.devices.takeIf { showDevices })

        if (prefs.dailyGb > 0) TodayPanel(d.today, gbToBytes(prefs.dailyGb), d.dailyLabel)

        Panel {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    when (period.kind) {
                        PeriodKind.DAY -> "By hour"
                        PeriodKind.ALL -> "By month"
                        else -> "By day"
                    },
                    Modifier.weight(1f), style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (showDevices) {
                    FilterChip(byDevice, { byDevice = true }, { Text("Devices") })
                    Spacer(Modifier.width(6.dp))
                    FilterChip(!byDevice, { byDevice = false }, { Text("↓ ↑") })
                }
            }
            Spacer(Modifier.height(8.dp))
            if (d.buckets.isNotEmpty()) {
                val deviceColors = d.devices.mapIndexed { i, dev -> deviceColor(i, dev.device) }
                UsageBars(
                    d.buckets,
                    parts = if (showDevices && byDevice) deviceColors else null,
                    limit = if (period.kind != PeriodKind.DAY && period.kind != PeriodKind.ALL &&
                        state.scope == prefs.dailyScope) gbToBytes(prefs.dailyGb) else 0,
                    labelEvery = when {
                        d.buckets.size > 24 -> 5
                        d.buckets.size > 12 -> 3
                        else -> 1
                    },
                    onOpen = { b ->
                        when (period.kind) {
                            PeriodKind.DAY -> Unit
                            PeriodKind.ALL -> state.open(PeriodKind.MONTH, b.start)
                            else -> state.open(PeriodKind.DAY, b.start)
                        }
                    },
                )
                Spacer(Modifier.height(10.dp))
                if (showDevices && byDevice) {
                    Row { d.devices.forEachIndexed { i, dev ->
                        LegendDot(deviceColors[i], deviceName(dev.device)); Spacer(Modifier.width(14.dp))
                    } }
                } else Legend(d.buckets.any { it.totals.unsplit > 0 })
            }
        }

        if (d.networks.isNotEmpty() && state.scope !is Scope.Named) {
            Panel(title = "Networks") {
                val max = d.networks.first().totals.total.coerceAtLeast(1)
                for (n in d.networks.take(5)) {
                    NetworkLine(n, max) { state.select(Scope.Named(n.net, n.key, n.label)) }
                }
                if (d.networks.size > 5) TextButton(onClick = openNetworks) { Text("All ${d.networks.size} networks") }
            }
        }

        if (d.apps.isNotEmpty()) {
            Panel(title = if (state.scope is Scope.Named) "Top apps on this phone, all ${if (state.scope.net == MOBILE) "mobile" else "Wi-Fi"}"
                else "Top apps on this phone") {
                val max = d.apps.first().totals.total.coerceAtLeast(1)
                val context = LocalContext.current
                for (a in d.apps) AppLine(a, max, AppNames.label(context, a.app))
                TextButton(onClick = openApps) { Text("All apps") }
            }
        }
        Spacer(Modifier.height(8.dp))
    }

    if (editIsp) IspDialog(state, d.plan, onDone = { editIsp = false; state.version++ })
}

/** A family phone sees only the bundle's totals, as the laptop last reported them. */
@Composable
private fun MemberPlan(json: String, version: Int) {
    val p = remember(json, version) { runCatching { org.json.JSONObject(json) }.getOrNull() }
    Panel {
        Text("Home bundle", style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (p == null) {
            Text("Connect to the home Wi-Fi to get the bundle's total.", Modifier.padding(top = 6.dp))
            return@Panel
        }
        val total = p.getLong("total")
        val used = p.getLong("used")
        val left = total - used
        Text(if (left >= 0) "${human(left, 1)} left" else "${human(-left, 1)} over",
            fontSize = 34.sp, fontWeight = FontWeight.Bold, color = if (left < 0) Danger else MaterialTheme.colorScheme.onSurface)
        Text("${human(used, 1)} used of ${human(total, 0)}", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(10.dp))
        StackedMeter(listOf(used to Teal), total)
        Spacer(Modifier.height(10.dp))
        // the laptop sends the day the next cycle starts, which is the renewal day
        val renews = LocalDate.parse(p.getString("renews"))
        val daysLeft = ChronoUnit.DAYS.between(LocalDate.now(), renews).coerceAtLeast(1)
        Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
            Stat("days left", "$daysLeft")
            Stat("safe per day", human(maxOf(left, 0) / daysLeft, 1))
            Stat("renews", renews.format(DateTimeFormatter.ofPattern("d MMM")))
        }
        Spacer(Modifier.height(8.dp))
        Text("For the whole home. Updated " + Instant.ofEpochSecond(p.optLong("updated")).atZone(ZoneId.systemDefault())
            .format(DateTimeFormatter.ofPattern("d MMM, HH:mm")), style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** The data plan, split into my usage (this phone and the laptop), each family phone and the rest. */
@Composable
private fun PlanHero(p: PlanStatus, label: String, onUpdate: () -> Unit) {
    val (pace, paceColor) = when {
        p.used > p.plan -> "Over plan" to Danger
        p.projected > p.plan -> "Fast pace" to Warn
        else -> "On track" to Ok
    }
    Panel {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Plan · $label", style = MaterialTheme.typography.titleSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
                Text(
                    if (p.remaining >= 0) "${human(p.remaining, 1)} left" else "${human(-p.remaining, 1)} over",
                    fontSize = 34.sp, fontWeight = FontWeight.Bold,
                    color = if (p.remaining < 0) Danger else MaterialTheme.colorScheme.onSurface,
                )
                Text("${human(p.used, 1)} used of ${human(p.plan, 0)}",
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Text(pace, color = paceColor, fontWeight = FontWeight.SemiBold)
        }
        Spacer(Modifier.height(12.dp))
        val parts = p.byDevice.filter { it.second > 0 }
        StackedMeter(parts.mapIndexed { i, (dev, v) -> v to deviceColor(p.byDevice.indexOfFirst { it.first == dev }, dev) }, p.plan)
        Spacer(Modifier.height(10.dp))
        // My usage is this phone and the laptop; then each family phone; then everyone else.
        val mine = p.byDevice.filter { it.first == PHONE || it.first == laptopDevice }
        val phones = p.byDevice.filter { it.first != PHONE && it.first != laptopDevice && it.first != OTHER_DEVICES }
        val others = p.byDevice.firstOrNull { it.first == OTHER_DEVICES }?.second ?: 0L
        PlanLine("My usage", mine.sumOf { it.second }, p.used, null, bold = true)
        for ((dev, v) in mine) {
            PlanLine(deviceName(dev), v, p.used, deviceColor(p.byDevice.indexOfFirst { it.first == dev }, dev), indent = true)
        }
        for ((dev, v) in phones) {
            PlanLine(dev, v, p.used, deviceColor(p.byDevice.indexOfFirst { it.first == dev }, dev), bold = true)
        }
        if (p.reportedAt != null) {
            Spacer(Modifier.height(4.dp))
            PlanLine("Others", others, p.used, FamilyColor, bold = true)
        }
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(18.dp)) {
            Stat("days left", "${p.daysLeft}")
            Stat("safe per day", human(p.perDayLeft, 1))
            Stat("at this pace", human(p.projected, 0))
            Stat("renews", p.cycle.end.plusDays(1).format(DateTimeFormatter.ofPattern("d MMM")))
        }
        Spacer(Modifier.height(10.dp))
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(
                p.reportedAt?.let {
                    "From your ISP on " + Instant.ofEpochSecond(it).atZone(ZoneId.systemDefault())
                        .format(DateTimeFormatter.ofPattern("d MMM, HH:mm")) + ", counted on since"
                } ?: "Only your phone and laptop so far. Enter the ISP's remaining quota to include the family.",
                Modifier.weight(1f), style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            OutlinedButton(onClick = onUpdate) { Text("Update from ISP") }
        }
    }
}

@Composable
private fun PlanLine(name: String, v: Long, used: Long, color: Color?, bold: Boolean = false, indent: Boolean = false) {
    Row(
        Modifier.fillMaxWidth().padding(start = if (indent) 18.dp else 0.dp, top = 3.dp, bottom = 3.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (color != null) {
            Canvas(Modifier.size(10.dp)) { drawCircle(color) }
            Spacer(Modifier.width(8.dp))
        }
        Text(name, Modifier.weight(1f), fontWeight = if (bold) FontWeight.SemiBold else FontWeight.Normal,
            color = if (indent) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.onSurface)
        Text(human(v, 1), fontWeight = if (bold) FontWeight.Bold else FontWeight.SemiBold)
        Text("  ${if (used > 0) v * 100 / used else 0}%", Modifier.width(48.dp),
            color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun StackedMeter(parts: List<Pair<Long, Color>>, total: Long) {
    val track = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.08f)
    Canvas(Modifier.fillMaxWidth().height(14.dp).clip(RoundedCornerShape(7.dp))) {
        drawRect(track)
        var x = 0f
        val sum = maxOf(total, parts.sumOf { it.first }, 1L).toFloat()
        for ((v, c) in parts) {
            val w = size.width * v / sum
            drawRect(c, Offset(x, 0f), Size(w, size.height))
            x += w
        }
    }
}

/** The ISP says how much is left; everything else follows from that. */
@Composable
private fun IspDialog(state: AppState, plan: PlanStatus?, onDone: () -> Unit) {
    val prefs = state.prefs
    var remaining by remember { mutableStateOf("") }
    var size by remember { mutableStateOf(prefs.planGb.let { if (it == Math.floor(it)) it.toLong().toString() else it.toString() }) }
    val cycleStart = Period.cycleOf(LocalDate.now(), prefs.cycleDay).start.toString()
    var extra by remember {
        mutableStateOf(if (prefs.extraCycle == cycleStart && prefs.extraGb > 0) prefs.extraGb.toString() else "")
    }
    val left = remaining.toDoubleOrNull()
    val total = size.toDoubleOrNull()
    AlertDialog(
        onDismissRequest = onDone,
        title = { Text("Update from ISP") },
        text = {
            Column {
                Text("Type the remaining quota from your ISP's message. Whatever it counted beyond " +
                    "your phone and laptop is your family's usage.",
                    style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(10.dp))
                OutlinedTextField(remaining, { remaining = it }, label = { Text("Remaining quota (GB)") },
                    singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
                Spacer(Modifier.height(6.dp))
                OutlinedTextField(size, { size = it }, label = { Text("Package size (GB)") },
                    singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
                Spacer(Modifier.height(6.dp))
                OutlinedTextField(extra, { extra = it }, label = { Text("Extra bundle this cycle (GB)") },
                    singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal))
                Text("Changes here reach the family's phones the next time they sync.",
                    Modifier.padding(top = 6.dp), style = MaterialTheme.typography.bodySmall)
                val extraGb = extra.toDoubleOrNull() ?: 0.0
                if (left != null && total != null) Text("Used so far: ${"%.1f".format(total + extraGb - left)} GB",
                    Modifier.padding(top = 6.dp), style = MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton = {
            val extraGb = extra.toDoubleOrNull() ?: 0.0
            TextButton(enabled = left != null && total != null && left <= total + extraGb, onClick = {
                prefs.planGb = total!!
                prefs.extraGb = extraGb
                prefs.extraCycle = if (extraGb > 0) cycleStart else ""
                // the ISP's remaining covers the package and any extra bundle together
                prefs.ispUsedGb = total + extraGb - left!!
                prefs.ispAt = System.currentTimeMillis() / 1000
                onDone()
            }) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDone) { Text("Cancel") } },
    )
}

@Composable
private fun TotalPanel(t: Totals, period: Period, devices: List<DeviceRow>?) {
    Panel {
        Text(human(t.total), fontSize = 36.sp, fontWeight = FontWeight.Bold)
        val perDay = if (period.days > 1) {
            val elapsed = minOf(period.days, ChronoUnit.DAYS.between(period.start, LocalDate.now()) + 1).coerceAtLeast(1)
            " · ${human(t.total / elapsed)} a day"
        } else ""
        Text("used$perDay", color = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.height(12.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(24.dp)) {
            Stat("download", "↓ ${human(t.rx)}", color = Down)
            Stat("upload", "↑ ${human(t.tx)}", color = Up)
            if (t.unsplit > 0) Stat("imported total", human(t.unsplit), color = Unsplit)
        }
        if (devices != null) {
            Spacer(Modifier.height(12.dp))
            val sum = devices.sumOf { it.totals.total }.coerceAtLeast(1)
            devices.forEachIndexed { i, dev ->
                Column(Modifier.padding(vertical = 4.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text(deviceName(dev.device), Modifier.weight(1f))
                        Text(human(dev.totals.total), fontWeight = FontWeight.SemiBold)
                        Text("  ${dev.totals.total * 100 / sum}%", Modifier.width(48.dp),
                            color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Spacer(Modifier.height(4.dp))
                    ShareBar(dev.totals.total.toFloat() / sum, deviceColor(i, dev.device))
                }
            }
        }
    }
}

@Composable
private fun TodayPanel(used: Long, limit: Long, label: String) {
    Panel(title = "Today · $label") {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Ring(used.toFloat() / limit, 72.dp, "${used * 100 / limit}%", "of ${human(limit, 0)}")
            Spacer(Modifier.width(16.dp))
            Column {
                Text(human(used), fontWeight = FontWeight.SemiBold, fontSize = 20.sp)
                Text(if (used > limit) "${human(used - limit)} over the daily limit" else "${human(limit - used)} left today",
                    color = if (used > limit) Danger else MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}
