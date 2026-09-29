package com.madboly.netusage.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.PathEffect
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.madboly.netusage.data.Bucket
import com.madboly.netusage.data.human
import java.time.LocalDate

/**
 * Stacked download / upload bars, or one segment per device when [parts] gives their colours. Days imported from My Data Manager only had a total,
 * which is drawn in grey. A tap selects a bar and shows its value; a second tap on the
 * same bar calls [onOpen].
 */
@Composable
fun UsageBars(
    buckets: List<Bucket>,
    modifier: Modifier = Modifier,
    limit: Long = 0,
    labelEvery: Int = 1,
    parts: List<Color>? = null,
    onOpen: ((Bucket) -> Unit)? = null,
) {
    val max = maxOf(buckets.maxOfOrNull { it.totals.total } ?: 0L, limit, 1L)
    var selected by remember(buckets) { mutableStateOf(-1) }
    val grid = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.10f)
    val currentColor = MaterialTheme.colorScheme.primary
    Column(modifier) {
        Box(Modifier.fillMaxWidth().height(20.dp)) {
            val b = buckets.getOrNull(selected)
            Text(
                if (b != null) "${b.label}: ${human(b.totals.total)}" else "Peak ${human(buckets.maxOfOrNull { it.totals.total } ?: 0)}",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Canvas(
            Modifier.fillMaxWidth().height(150.dp).pointerInput(buckets) {
                detectTapGestures { pos ->
                    val i = (pos.x / (size.width.toFloat() / buckets.size)).toInt().coerceIn(0, buckets.lastIndex)
                    if (i == selected) onOpen?.invoke(buckets[i]) else selected = i
                }
            }
        ) {
            val slot = size.width / buckets.size
            val bw = (slot * 0.66f).coerceAtMost(28.dp.toPx())
            for (f in listOf(0.5f, 1f)) {
                val y = size.height * (1 - f)
                drawLine(grid, Offset(0f, y), Offset(size.width, y), 1.dp.toPx())
            }
            drawLine(grid, Offset(0f, size.height), Offset(size.width, size.height), 1.dp.toPx())
            buckets.forEachIndexed { i, b ->
                val x = i * slot + (slot - bw) / 2
                var y = size.height
                val dim = selected >= 0 && selected != i
                val stack = if (parts != null) b.parts.mapIndexed { j, v -> v to parts[j % parts.size] }
                else listOf(b.totals.rx to Down, b.totals.tx to Up, b.totals.unsplit to Unsplit)
                for ((v, c) in stack) {
                    if (v <= 0) continue
                    val h = (v.toFloat() / max * size.height).coerceAtLeast(1.5f)
                    drawRoundRect(
                        c.copy(alpha = if (dim) 0.35f else 1f),
                        Offset(x, y - h), Size(bw, h), CornerRadius(3.dp.toPx())
                    )
                    y -= h
                }
                if (b.current) drawCircle(currentColor, 2.5.dp.toPx(), Offset(x + bw / 2, size.height + 5.dp.toPx()))
            }
            if (limit > 0) {
                val y = size.height * (1 - limit.toFloat() / max)
                drawLine(Danger.copy(alpha = 0.8f), Offset(0f, y), Offset(size.width, y), 1.5.dp.toPx(),
                    pathEffect = PathEffect.dashPathEffect(floatArrayOf(10f, 8f)))
            }
        }
        Spacer(Modifier.height(8.dp))
        Row(Modifier.fillMaxWidth()) {
            buckets.forEachIndexed { i, b ->
                Box(Modifier.weight(1f), contentAlignment = Alignment.Center) {
                    if (i % labelEvery == 0) Text(b.label, fontSize = 9.sp, maxLines = 1,
                        color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }
    }
}

@Composable
fun Legend(unsplit: Boolean) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        LegendDot(Down, "Download")
        Spacer(Modifier.width(14.dp))
        LegendDot(Up, "Upload")
        if (unsplit) {
            Spacer(Modifier.width(14.dp))
            LegendDot(Unsplit, "Imported total")
        }
    }
}

@Composable
fun LegendDot(color: Color, text: String) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Canvas(Modifier.size(9.dp)) { drawCircle(color) }
        Spacer(Modifier.width(5.dp))
        Text(text, style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** Progress ring, green until 80%, orange to 100%, red beyond. */
@Composable
fun Ring(fraction: Float, size: Dp, center: String, sub: String) {
    val track = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.10f)
    val color = when {
        fraction >= 1f -> Danger
        fraction >= 0.8f -> Warn
        else -> Ok
    }
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.size(size)) {
            val w = 10.dp.toPx()
            val inset = w / 2
            val arc = Size(this.size.width - w, this.size.height - w)
            drawArc(track, -90f, 360f, false, Offset(inset, inset), arc, style = Stroke(w))
            drawArc(color, -90f, 360f * fraction.coerceIn(0f, 1f), false, Offset(inset, inset), arc,
                style = Stroke(w, cap = StrokeCap.Round))
        }
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(center, fontWeight = FontWeight.SemiBold, fontSize = 16.sp)
            Text(sub, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

/** Thin horizontal share bar. */
@Composable
fun ShareBar(fraction: Float, color: Color, modifier: Modifier = Modifier) {
    val track = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.08f)
    Canvas(modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp))) {
        drawRect(track)
        drawRect(color, size = Size(size.width * fraction.coerceIn(0f, 1f), size.height))
    }
}

/**
 * GitHub-style calendar of daily usage, one column per week, most recent on the right.
 * Days over [limit] are red.
 */
@Composable
fun CalendarHeat(days: Map<LocalDate, Long>, weeks: Int, limit: Long, onTap: (LocalDate) -> Unit) {
    val today = LocalDate.now()
    val end = today.plusDays((7 - today.dayOfWeek.value % 7 - 1).toLong().coerceAtLeast(0))
    val start = end.minusDays(weeks * 7L - 1)
    val max = maxOf(days.values.maxOrNull() ?: 1L, 1L)
    val empty = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.06f)
    Canvas(
        Modifier.fillMaxWidth().height(96.dp)
            .pointerInput(days) {
                detectTapGestures { pos ->
                    val cell = size.width.toFloat() / weeks
                    val col = (pos.x / cell).toInt()
                    val row = (pos.y / (size.height / 7f)).toInt().coerceIn(0, 6)
                    val d = start.plusDays(col * 7L + row)
                    if (!d.isAfter(today)) onTap(d)
                }
            }
    ) {
        val cell = size.width / weeks
        val ch = size.height / 7
        val s = minOf(cell, ch) * 0.82f
        for (i in 0 until weeks * 7) {
            val d = start.plusDays(i.toLong())
            if (d.isAfter(today)) continue
            val v = days[d] ?: 0L
            val color = when {
                v == 0L -> empty
                limit in 1 until v -> Danger
                else -> Teal.copy(alpha = 0.25f + 0.75f * (v.toFloat() / max))
            }
            drawRoundRect(color, Offset((i / 7) * cell, (i % 7) * ch), Size(s, s), CornerRadius(2.dp.toPx()))
        }
    }
}
