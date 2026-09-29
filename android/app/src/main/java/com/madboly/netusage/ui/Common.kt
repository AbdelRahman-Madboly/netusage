package com.madboly.netusage.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.filled.KeyboardArrowRight
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.foundation.layout.height
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.Prefs
import com.madboly.netusage.data.Repo
import com.madboly.netusage.data.Scope
import com.madboly.netusage.data.step
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.time.LocalDate

/** What every tab looks at: which traffic, which period, and a counter bumped on each sync. */
class AppState(val repo: Repo, val prefs: Prefs, val household: Repo) {
    var scope by mutableStateOf(prefs.viewScope)
    var devicesMode by mutableStateOf(prefs.devicesMode)
    val withDevices: Boolean get() = devicesMode != "phone"
    var scopeLabel by mutableStateOf("")
    var kind by mutableStateOf(PeriodKind.CYCLE)
    var anchor by mutableStateOf(LocalDate.now())
    var version by mutableIntStateOf(0)
    var firstDay: LocalDate = LocalDate.now()

    val period: Period get() = Period.of(kind, anchor, prefs.cycleDay, firstDay)
    val isCurrent: Boolean get() = period.contains(LocalDate.now()) || kind == PeriodKind.ALL

    fun select(scope: Scope) {
        this.scope = scope
        prefs.viewScope = scope
    }

    /** What to add to this phone's usage: "all" devices, "mine" (the laptop) or "phone" only. */
    fun showDevices(mode: String) {
        devicesMode = mode
        prefs.devicesMode = mode
        repo.devicesMode = mode
        version++
    }

    fun open(kind: PeriodKind, day: LocalDate) {
        this.kind = kind
        anchor = day
    }
}

/** Loads [block] off the main thread whenever any of [keys] changes. */
@Composable
fun <T> load(initial: T, vararg keys: Any?, block: () -> T): T {
    val v by produceState(initial, *keys) { value = withContext(Dispatchers.IO) { block() } }
    return v
}

@Composable
fun Panel(modifier: Modifier = Modifier, title: String? = null, content: @Composable ColumnScope.() -> Unit) {
    Card(
        modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer),
        shape = MaterialTheme.shapes.large,
    ) {
        Column(Modifier.padding(16.dp)) {
            if (title != null) {
                Text(title, style = MaterialTheme.typography.titleSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                androidx.compose.foundation.layout.Spacer(Modifier.padding(top = 10.dp))
            }
            content()
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PeriodBar(state: AppState, kinds: List<PeriodKind> = PeriodKind.entries) {
    Column {
        SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
            kinds.forEachIndexed { i, k ->
                SegmentedButton(
                    selected = state.kind == k,
                    onClick = { state.kind = k; state.anchor = LocalDate.now() },
                    shape = SegmentedButtonDefaults.itemShape(i, kinds.size),
                    icon = {},
                    label = { Text(k.label, maxLines = 1, style = MaterialTheme.typography.labelMedium) },
                )
            }
        }
        Row(Modifier.fillMaxWidth().height(48.dp), verticalAlignment = Alignment.CenterVertically) {
            if (state.kind != PeriodKind.ALL) {
                IconButton(onClick = { state.anchor = step(state.period, -1) }) {
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowLeft, "Previous")
                }
            }
            Text(
                state.period.label, Modifier.weight(1f), textAlign = TextAlign.Center,
                style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold,
            )
            if (state.kind != PeriodKind.ALL) {
                IconButton(onClick = { state.anchor = step(state.period, 1) }, enabled = !state.isCurrent) {
                    Icon(Icons.AutoMirrored.Filled.KeyboardArrowRight, "Next")
                }
            }
        }
    }
}

@Composable
fun Stat(label: String, value: String, modifier: Modifier = Modifier, color: androidx.compose.ui.graphics.Color? = null) {
    Column(modifier) {
        Text(value, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold,
            color = color ?: MaterialTheme.colorScheme.onSurface)
        Text(label, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
