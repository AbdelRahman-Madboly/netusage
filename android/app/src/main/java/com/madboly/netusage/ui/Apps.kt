package com.madboly.netusage.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.madboly.netusage.data.AppNames
import com.madboly.netusage.data.AppRow
import com.madboly.netusage.data.Bucket
import com.madboly.netusage.data.MOBILE
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.Scope
import com.madboly.netusage.data.Totals
import com.madboly.netusage.data.human
import java.time.format.DateTimeFormatter

private data class Named(val row: AppRow, val label: String, val isNew: Boolean)

@Composable
fun AppsScreen(state: AppState) {
    val context = LocalContext.current
    val period = state.period
    var query by remember { mutableStateOf("") }
    var byName by remember { mutableStateOf(false) }
    var open by remember { mutableStateOf<Named?>(null) }
    val data = load(Pair(emptyList<Named>(), null as java.time.LocalDate?), state.scope, period, state.version) {
        val start = state.repo.appDataStart()
        state.repo.apps(state.scope, period).map { r ->
            val first = state.repo.appFirstSeen(r.app)
            Named(r, AppNames.label(context, r.app),
                first != null && start != null && first.isAfter(start) && period.contains(first))
        } to start
    }
    val rows = data.first
        .filter { query.isBlank() || it.label.contains(query, true) || it.row.app.contains(query, true) }
        .let { if (byName) it.sortedBy { n -> n.label.lowercase() } else it }
    val total = data.first.sumOf { it.row.totals.total }.coerceAtLeast(1)
    val max = data.first.maxOfOrNull { it.row.totals.total }?.coerceAtLeast(1) ?: 1

    LazyColumn(Modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(2.dp)) {
        item { PeriodBar(state) }
        item {
            OutlinedTextField(
                query, { query = it }, Modifier.fillMaxWidth(), singleLine = true,
                leadingIcon = { Icon(Icons.Default.Search, null) }, placeholder = { Text("Search apps") },
            )
            Row(Modifier.padding(vertical = 8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically) {
                FilterChip(!byName, { byName = false }, { Text("By usage") })
                FilterChip(byName, { byName = true }, { Text("By name") })
                Spacer(Modifier.weight(1f))
                Text("${rows.size} apps", style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            if (state.scope is Scope.Named) Text(
                "Android counts apps per network type, not per router or SIM, so this shows every " +
                    (if (state.scope.net == MOBILE) "mobile" else "Wi-Fi") + " network.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(bottom = 8.dp),
            )
            val start = data.second
            if (start != null && period.start.isBefore(start)) Text(
                "Per-app numbers start ${start.format(DateTimeFormatter.ofPattern("d MMM yyyy"))}; " +
                    "My Data Manager's older history only had totals.",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(bottom = 8.dp),
            )
        }
        items(rows, key = { it.row.app }) { n ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) { AppLine(n.row, max, n.label) { open = n } }
                if (n.isNew) {
                    Spacer(Modifier.width(6.dp))
                    AssistChip(onClick = { open = n }, label = { Text("new") })
                }
            }
        }
        item {
            Text("${human(total)} across all apps", Modifier.padding(vertical = 16.dp),
                style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }

    open?.let { n ->
        val rank = data.first.indexOfFirst { it.row.app == n.row.app } + 1
        val chartPeriod = if (period.kind == PeriodKind.DAY || period.kind == PeriodKind.ALL)
            Period.cycleOf(period.end, state.prefs.cycleDay) else period
        val daily = load(emptyMap<java.time.LocalDate, Totals>(), n.row.app, chartPeriod, state.scope) {
            state.repo.appDaily(n.row.app, state.scope, chartPeriod)
        }
        val first = load<java.time.LocalDate?>(null, n.row.app) { state.repo.appFirstSeen(n.row.app) }
        AlertDialog(
            onDismissRequest = { open = null },
            title = { Text(n.label) },
            text = {
                Column {
                    Text(human(n.row.totals.total), style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                    Text("#$rank of ${data.first.size} · ${n.row.totals.total * 100 / total}% of ${period.label}",
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(20.dp)) {
                        Stat("download", "↓ ${human(n.row.totals.rx)}", color = Down)
                        Stat("upload", "↑ ${human(n.row.totals.tx)}", color = Up)
                    }
                    Spacer(Modifier.height(12.dp))
                    Text("Daily, ${chartPeriod.label}", style = MaterialTheme.typography.labelMedium)
                    UsageBars(
                        (0 until chartPeriod.days).map {
                            val d = chartPeriod.start.plusDays(it)
                            Bucket(d.dayOfMonth.toString(), d, daily[d] ?: Totals(), d == java.time.LocalDate.now())
                        },
                        labelEvery = 5,
                    )
                    first?.let {
                        Spacer(Modifier.height(8.dp))
                        Text("First seen ${it.format(DateTimeFormatter.ofPattern("d MMM yyyy"))} · ${n.row.app}",
                            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            },
            confirmButton = { TextButton(onClick = { open = null }) { Text("Close") } },
        )
    }
}
