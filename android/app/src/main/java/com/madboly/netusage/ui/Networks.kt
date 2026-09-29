package com.madboly.netusage.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
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
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.madboly.netusage.R
import com.madboly.netusage.data.MOBILE
import com.madboly.netusage.data.NetworkRow
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.Scope
import com.madboly.netusage.data.WIFI
import com.madboly.netusage.data.WIRED
import com.madboly.netusage.data.human
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter

@Composable
fun NetworksScreen(state: AppState, onPick: () -> Unit) {
    val period = state.period
    val rows = load(emptyList(), period, state.version) { state.repo.networks(period) }
    val all = load(emptyList(), state.version) {
        state.repo.networks(Period(PeriodKind.ALL, state.repo.firstDay(), LocalDate.now()))
    }
    var renaming by remember { mutableStateOf<NetworkRow?>(null) }
    var merging by remember { mutableStateOf<NetworkRow?>(null) }

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        PeriodBar(state)
        for ((net, title) in listOf(WIFI to "Wi-Fi", MOBILE to "Mobile", WIRED to "Wired")) {
            val list = rows.filter { it.net == net }
            if (list.isEmpty()) continue
            val sum = list.sumOf { it.totals.total }
            Panel(title = "$title · ${human(sum)}") {
                val max = list.first().totals.total.coerceAtLeast(1)
                for (n in list) {
                    NetworkCard(
                        n, max, sum,
                        onClick = { state.select(Scope.Named(n.net, n.key, n.label)); onPick() },
                        onRename = { renaming = n },
                        onMerge = { merging = n },
                        onSeparate = { n.keys.forEach { state.repo.unmerge(it) }; state.version++ },
                    )
                }
            }
        }
        Text(
            "Wi-Fi is tracked by router, so a renamed network or a new password stays under the same entry. " +
                (if (state.prefs.isMember) "" else "With other devices shown, a router they share is one entry holding all their usage. ") +
                "History from before Net Usage was installed only knew network names; those join their router " +
                "the first time it is seen with that name. Use Merge for anything that should be counted together.",
            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
    }

    renaming?.let { n ->
        var text by remember(n) { mutableStateOf(n.label) }
        AlertDialog(
            onDismissRequest = { renaming = null },
            title = { Text("Rename") },
            text = { OutlinedTextField(text, { text = it }, singleLine = true, label = { Text("Shown as") }) },
            confirmButton = {
                TextButton(onClick = { state.repo.setCustomLabel(n.key, text); renaming = null; state.version++ }) { Text("Save") }
            },
            dismissButton = { TextButton(onClick = { renaming = null }) { Text("Cancel") } },
        )
    }

    merging?.let { n ->
        val targets = all.filter { it.net == n.net && it.key != n.key }
        AlertDialog(
            onDismissRequest = { merging = null },
            title = { Text("Count \"${n.label}\" as…") },
            text = {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    if (targets.isEmpty()) Text("Nothing else of the same kind to merge with.")
                    for (t in targets) {
                        Row(
                            Modifier.fillMaxWidth().clickable {
                                state.repo.merge(n.key, t.key); merging = null; state.version++
                            }.padding(vertical = 12.dp)
                        ) {
                            Text(t.label, Modifier.weight(1f))
                            Text(human(t.totals.total), color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            },
            confirmButton = { TextButton(onClick = { merging = null }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun NetworkCard(
    n: NetworkRow, max: Long, sum: Long,
    onClick: () -> Unit, onRename: () -> Unit, onMerge: () -> Unit, onSeparate: () -> Unit,
) {
    val color = if (n.net == MOBILE) MobileColor else WifiColor
    var menu by remember { mutableStateOf(false) }
    Column(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 8.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(painterResource(if (n.net == MOBILE) R.drawable.ic_sim else R.drawable.ic_router), null,
                Modifier.size(22.dp), tint = color)
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(n.label, fontWeight = FontWeight.SemiBold)
                val others = n.names.filter { it != n.label }
                if (others.isNotEmpty()) Text("Also: ${others.joinToString(", ")}",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                val seen = if (n.lastSeen > 0) "Last used " + Instant.ofEpochSecond(n.lastSeen).atZone(ZoneId.systemDefault())
                    .format(DateTimeFormatter.ofPattern("d MMM, HH:mm")) else "Not seen by this phone"
                Text("$seen · ${if (sum > 0) n.totals.total * 100 / sum else 0}%",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            Text(human(n.totals.total), fontWeight = FontWeight.SemiBold)
            if (n.key.isNotEmpty()) Box {
                IconButton(onClick = { menu = true }) { Icon(Icons.Default.MoreVert, "More") }
                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                    DropdownMenuItem(text = { Text("Rename") }, onClick = { menu = false; onRename() })
                    DropdownMenuItem(text = { Text("Merge into…") }, onClick = { menu = false; onMerge() })
                    if (n.keys.size > 1) DropdownMenuItem(text = { Text("Separate") }, onClick = { menu = false; onSeparate() })
                }
            }
        }
        Spacer(Modifier.height(6.dp))
        ShareBar(n.totals.total.toFloat() / max, color)
    }
}
