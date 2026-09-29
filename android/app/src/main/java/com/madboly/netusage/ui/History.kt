package com.madboly.netusage.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.gbToBytes
import com.madboly.netusage.data.human
import java.time.LocalDate
import java.time.YearMonth
import java.time.format.DateTimeFormatter

private class HistoryData(
    val cycles: List<Pair<Period, Long>> = emptyList(),
    val daily: Map<LocalDate, Long> = emptyMap(),
    val months: List<Pair<YearMonth, Long>> = emptyList(),
)

@Composable
fun HistoryScreen(state: AppState, openDay: () -> Unit) {
    val prefs = state.prefs
    val d = load(HistoryData(), state.scope, state.version, prefs.cycleDay) {
        val first = state.repo.firstDay()
        val daily = state.repo.daily(state.scope, first, LocalDate.now()).mapValues { it.value.total }
        val cycles = buildList {
            var c = Period.cycleOf(LocalDate.now(), prefs.cycleDay)
            while (!c.end.isBefore(first) && size < 12) {
                add(c to (0 until c.days).sumOf { daily[c.start.plusDays(it)] ?: 0L })
                c = Period.cycleOf(c.start.minusDays(1), prefs.cycleDay)
            }
        }
        val months = daily.entries.groupBy({ YearMonth.from(it.key) }, { it.value })
            .map { it.key to it.value.sum() }.sortedByDescending { it.first }
        HistoryData(cycles, daily, months)
    }
    val fmt = DateTimeFormatter.ofPattern("d MMM")

    Column(
        Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Spacer(Modifier.height(4.dp))
        Panel(title = "Billing cycles (from day ${prefs.cycleDay})") {
            val max = d.cycles.maxOfOrNull { it.second }?.coerceAtLeast(1) ?: 1
            val plan = if (prefs.planScope == state.scope) gbToBytes(prefs.planGb) else 0
            for ((c, total) in d.cycles) {
                Column(Modifier.fillMaxWidth().clickable { state.open(PeriodKind.CYCLE, c.start); openDay() }.padding(vertical = 6.dp)) {
                    Row {
                        Text("${c.start.format(fmt)} – ${c.end.format(fmt)}", Modifier.weight(1f))
                        Text(human(total), fontWeight = FontWeight.SemiBold,
                            color = if (plan in 1 until total) Danger else MaterialTheme.colorScheme.onSurface)
                    }
                    Spacer(Modifier.height(4.dp))
                    ShareBar(total.toFloat() / max, if (c.contains(LocalDate.now())) Mint else Teal)
                }
            }
        }

        Panel(title = "Last 6 months") {
            CalendarHeat(d.daily, 26, if (prefs.dailyScope == state.scope) gbToBytes(prefs.dailyGb) else 0) { day ->
                state.open(PeriodKind.DAY, day); openDay()
            }
            Text("Tap a day to open it", style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant)
        }

        Panel(title = "Busiest days") {
            val top = d.daily.entries.sortedByDescending { it.value }.take(10)
            top.forEachIndexed { i, (day, v) ->
                Row(Modifier.fillMaxWidth().clickable { state.open(PeriodKind.DAY, day); openDay() }.padding(vertical = 5.dp)) {
                    Text("${i + 1}", Modifier.width(28.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(day.format(DateTimeFormatter.ofPattern("EEE d MMM yyyy")), Modifier.weight(1f))
                    Text(human(v), fontWeight = FontWeight.SemiBold)
                }
            }
        }

        Panel(title = "By month") {
            val max = d.months.maxOfOrNull { it.second }?.coerceAtLeast(1) ?: 1
            for ((m, v) in d.months) {
                Column(Modifier.fillMaxWidth().clickable { state.open(PeriodKind.MONTH, m.atDay(1)); openDay() }.padding(vertical = 6.dp)) {
                    Row {
                        Text(m.format(DateTimeFormatter.ofPattern("MMMM yyyy")), Modifier.weight(1f))
                        Text(human(v), fontWeight = FontWeight.SemiBold)
                    }
                    Spacer(Modifier.height(4.dp))
                    ShareBar(v.toFloat() / max, Teal)
                }
            }
        }
        Spacer(Modifier.height(8.dp))
    }
}
