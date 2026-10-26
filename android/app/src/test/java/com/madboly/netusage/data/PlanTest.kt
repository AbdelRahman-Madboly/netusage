package com.madboly.netusage.data

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.LocalDate

class PlanTest {
    private val cycle = Period.cycleOf(LocalDate.parse("2026-10-26"), 27)   // 27 Sep - 26 Oct, 30 days
    private val gb = 1L shl 30
    private fun status(used: Long) = PlanStatus(cycle, 510 * gb, used, emptyList(), null)

    @Test fun projectionCountsOnlyTheTimeThatHasPassed() {
        // 18:00 on the last day: 29.75 of 30 days gone, so the pace still adds a little
        val at = cycle.endEpoch - 6 * 3600
        assertEquals(30 * gb, status(29_750 * gb / 1000).projectedAt(at))
    }

    @Test fun projectionHalfwayDoublesTheUsage() {
        val at = cycle.startEpoch + 15 * 86_400
        assertEquals(200 * gb, status(100 * gb).projectedAt(at))
    }

    @Test fun projectionInTheFirstHourDoesNotExplode() {
        assertEquals(30 * gb, status(gb).projectedAt(cycle.startEpoch))
    }
}
