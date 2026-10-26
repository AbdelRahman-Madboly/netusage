package com.madboly.netusage.ui

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.madboly.netusage.BuildConfig
import com.madboly.netusage.data.Alerts
import com.madboly.netusage.data.Laptop
import com.madboly.netusage.data.MOBILE
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.Prefs
import com.madboly.netusage.data.Scope
import com.madboly.netusage.data.Sync
import com.madboly.netusage.data.Tracker
import com.madboly.netusage.data.WIFI
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SettingsScreen(state: AppState, onSync: () -> Unit) {
    val context = LocalContext.current
    val prefs = state.prefs
    // Bumped to re-check permissions, e.g. after coming back from system settings.
    var tick by remember { mutableIntStateOf(0) }
    val lifecycle = androidx.compose.ui.platform.LocalLifecycleOwner.current
    androidx.compose.runtime.DisposableEffect(lifecycle) {
        val obs = androidx.lifecycle.LifecycleEventObserver { _, e ->
            if (e == androidx.lifecycle.Lifecycle.Event.ON_RESUME) tick++
        }
        lifecycle.lifecycle.addObserver(obs)
        onDispose { lifecycle.lifecycle.removeObserver(obs) }
    }
    val groups = load(emptyList(), state.version) {
        state.repo.networks(Period(PeriodKind.ALL, state.repo.firstDay(), LocalDate.now()))
    }
    val scopes = listOf(Scope.All, Scope.Type(MOBILE), Scope.Type(WIFI)) +
        groups.map { Scope.Named(it.net, it.key, it.label) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Spacer(Modifier.height(4.dp))
        Panel(title = "Access") {
            key(tick) {
                Permission("Usage access", "Needed to read how much each app and network used",
                    Sync.hasUsageAccess(context)) {
                    context.startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS, Uri.parse("package:${context.packageName}")))
                }
                // Android 9 has no separate "all the time" permission: location covers it
                Permission("Location, all the time", "Android only shows the Wi-Fi name and router with it",
                    granted(context, if (android.os.Build.VERSION.SDK_INT >= 29) Manifest.permission.ACCESS_BACKGROUND_LOCATION
                        else Manifest.permission.ACCESS_FINE_LOCATION)) { openAppSettings(context) }
                Permission("Phone", "Tells the two SIMs apart", granted(context, Manifest.permission.READ_PHONE_STATE)) {
                    openAppSettings(context)
                }
                Permission("Notifications", "Limit warnings",
                    if (android.os.Build.VERSION.SDK_INT >= 33) granted(context, Manifest.permission.POST_NOTIFICATIONS)
                    else androidx.core.app.NotificationManagerCompat.from(context).areNotificationsEnabled()) {
                    openAppSettings(context)
                }
                val pm = context.getSystemService(PowerManager::class.java)
                Permission("Unrestricted battery", "Keeps tracking running in the background",
                    pm.isIgnoringBatteryOptimizations(context.packageName)) {
                    context.startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:${context.packageName}")))
                }
            }
        }

        var planGb by remember { mutableStateOf(prefs.planGb) }
        var cycleDay by remember { mutableIntStateOf(prefs.cycleDay) }
        var planScope by remember { mutableStateOf(prefs.planScope) }
        var planPct by remember { mutableStateOf(prefs.planPercents) }
        var dailyGb by remember { mutableStateOf(prefs.dailyGb) }
        var dailyScope by remember { mutableStateOf(prefs.dailyScope) }
        var dailyPct by remember { mutableStateOf(prefs.dailyPercents) }
        var repeat by remember { mutableIntStateOf(prefs.repeatMinutes) }

        if (!prefs.isMember) {
        Panel(title = "Data plan") {
            NumberField("Plan size (GB, 0 for none)", planGb) { planGb = it; prefs.planGb = it; state.version++ }
            NumberField("Renews on day of month", cycleDay.toDouble(), whole = true) {
                cycleDay = it.toInt().coerceIn(1, 31); prefs.cycleDay = cycleDay; state.version++
            }
            ScopeChoice("Counts", planScope, scopes, state) { planScope = it; prefs.planScope = it; state.version++ }
            Text("Warn at", style = MaterialTheme.typography.labelLarge)
            Percents(Prefs.PLAN_CHOICES, planPct) { planPct = it; prefs.planPercents = it }
        }
        }

        Panel(title = "Daily limit") {
            NumberField("Limit per day (GB, 0 for none)", dailyGb) { dailyGb = it; prefs.dailyGb = it; state.version++ }
            ScopeChoice("Counts", dailyScope, scopes, state) { dailyScope = it; prefs.dailyScope = it; state.version++ }
            Text("Warn at", style = MaterialTheme.typography.labelLarge)
            Percents(Prefs.DAILY_CHOICES, dailyPct) { dailyPct = it; prefs.dailyPercents = it }
        }

        Panel(title = "Reminders") {
            Text("Once past a limit, remind me every", style = MaterialTheme.typography.bodyMedium)
            FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                for (m in Prefs.REPEAT_CHOICES) FilterChip(
                    repeat == m, { repeat = m; prefs.repeatMinutes = m },
                    { Text(if (m == 0) "Off" else if (m < 60) "$m min" else "${m / 60} h") },
                )
            }
            OutlinedButton(onClick = {
                Alerts.notify(context, 9, "Net Usage", "Notifications are working")
            }) { Text("Send a test notification") }
        }

        if (!prefs.isMember) {
        Panel(title = "Laptop") {
            var host by remember { mutableStateOf(prefs.laptopHost) }
            var token by remember { mutableStateOf(prefs.laptopToken) }
            var result by remember { mutableStateOf("") }
            val scope = androidx.compose.runtime.rememberCoroutineScope()
            val info = load(Triple(0L, "", ""), state.version, tick) {
                Triple(state.repo.lastLaptopSync(), state.repo.laptopName() ?: "", state.repo.laptopError())
            }
            Text(
                when {
                    token.isEmpty() -> "Not linked. Paste the token from the laptop's /var/lib/netusage/share.token."
                    info.first == 0L -> "Not reached yet" + (if (info.third.isNotEmpty()) ": ${info.third}" else "")
                    else -> "${info.second.ifEmpty { "Laptop" }} · last synced " +
                        Instant.ofEpochSecond(info.first).atZone(ZoneId.systemDefault())
                            .format(DateTimeFormatter.ofPattern("d MMM, HH:mm")) +
                        (if (info.third.isNotEmpty()) " · ${info.third}" else "")
                },
                style = MaterialTheme.typography.bodyMedium,
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(host, { host = it; prefs.laptopHost = it }, Modifier.fillMaxWidth(),
                label = { Text("Address (found automatically if it changes)") }, singleLine = true)
            Spacer(Modifier.height(6.dp))
            OutlinedTextField(token, { token = it; prefs.laptopToken = it }, Modifier.fillMaxWidth(),
                label = { Text("Token") }, singleLine = true,
                visualTransformation = androidx.compose.ui.text.input.PasswordVisualTransformation())
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedButton(onClick = {
                    result = "Syncing…"
                    scope.launch {
                        val err = withContext(Dispatchers.IO) { Laptop.sync(context) }
                        result = err ?: "Synced"
                        host = prefs.laptopHost
                        state.version++
                    }
                }, enabled = token.isNotEmpty()) { Text("Sync laptop now") }
                Spacer(Modifier.width(12.dp))
                Text(result, style = MaterialTheme.typography.bodySmall)
            }
            Text(
                "Reads the laptop's usage over the home Wi-Fi when both are on it. Data plans and " +
                    "limits always count both devices.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        } else {
            Panel(title = "Family phone") {
                val info = load(Pair(0L, ""), state.version, tick) {
                    state.repo.lastLaptopSync() to state.repo.laptopError()
                }
                Text("This phone reports to the home laptop as \"${prefs.deviceName}\". It shows only " +
                    "its own usage, and the home bundle's total from the laptop.")
                Spacer(Modifier.height(6.dp))
                Text(
                    if (info.first == 0L) "Not reached the laptop yet" + (if (info.second.isNotEmpty()) ": ${info.second}" else "")
                    else "Last synced " + Instant.ofEpochSecond(info.first).atZone(ZoneId.systemDefault())
                        .format(DateTimeFormatter.ofPattern("d MMM, HH:mm")),
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        Panel(title = "Status") {
            val fmt = DateTimeFormatter.ofPattern("d MMM, HH:mm:ss")
            fun at(sec: Long) = if (sec == 0L) "never" else Instant.ofEpochSecond(sec).atZone(ZoneId.systemDefault()).format(fmt)
            val info = load(Triple(0L, 0L, ""), state.version, tick) {
                Triple(state.repo.lastSync(), state.repo.lastSample(), "${state.repo.firstDay()}")
            }
            Text("Last sync: ${at(info.first)}")
            Text("Tracker last looked: ${at(info.second)}")
            Text("History starts: ${info.third}")
            Text("App version: ${BuildConfig.VERSION_NAME}")
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedButton(onClick = onSync) { Text("Sync now") }
                OutlinedButton(onClick = { Tracker.start(context); tick++ }) { Text("Restart tracker") }
            }
            Text(
                "Imported from My Data Manager: daily totals from 10 Mar 2026 and weekly per-app totals " +
                    "until Android's own history begins on 22 Jun 2026.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(8.dp))
    }
}

@Composable
private fun key(k: Int, content: @Composable () -> Unit) = androidx.compose.runtime.key(k) { content() }

private fun granted(context: Context, perm: String) =
    ContextCompat.checkSelfPermission(context, perm) == PackageManager.PERMISSION_GRANTED

private fun openAppSettings(context: Context) =
    context.startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:${context.packageName}")))

@Composable
private fun Permission(title: String, why: String, ok: Boolean, fix: () -> Unit) {
    Row(Modifier.fillMaxWidth().clickable(enabled = !ok, onClick = fix).padding(vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically) {
        Icon(if (ok) Icons.Default.CheckCircle else Icons.Default.Warning, null,
            Modifier.size(22.dp), tint = if (ok) Ok else Warn)
        Spacer(Modifier.width(12.dp))
        Column(Modifier.weight(1f)) {
            Text(title, fontWeight = FontWeight.SemiBold)
            Text(why, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (!ok) TextButton(onClick = fix) { Text("Allow") }
    }
}

@Composable
private fun NumberField(label: String, value: Double, whole: Boolean = false, onSave: (Double) -> Unit) {
    fun show(v: Double) = if (whole || v == Math.floor(v)) v.toLong().toString() else v.toString()
    var text by remember { mutableStateOf(show(value)) }
    OutlinedTextField(
        text, { text = it; it.toDoubleOrNull()?.takeIf { v -> v >= 0 }?.let(onSave) },
        Modifier.fillMaxWidth().padding(bottom = 8.dp), label = { Text(label) }, singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = if (whole) KeyboardType.Number else KeyboardType.Decimal),
    )
}

@Composable
private fun ScopeChoice(label: String, current: Scope, options: List<Scope>, state: AppState, onPick: (Scope) -> Unit) {
    var open by remember { mutableStateOf(false) }
    val currentLabel = load(current.label, current, state.version) { state.repo.labelFor(current) }
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(bottom = 4.dp)) {
        Text(label, Modifier.weight(1f))
        androidx.compose.foundation.layout.Box {
            OutlinedButton(onClick = { open = true }) { Text(currentLabel) }
            DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                for (s in options) DropdownMenuItem(
                    text = { Text(if (s is Scope.Named) s.title else s.label) },
                    onClick = { onPick(s); open = false },
                )
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun Percents(choices: List<Int>, selected: Set<Int>, onChange: (Set<Int>) -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        for (p in choices) FilterChip(
            p in selected, { onChange(if (p in selected) selected - p else selected + p) }, { Text("$p%") },
        )
    }
}
