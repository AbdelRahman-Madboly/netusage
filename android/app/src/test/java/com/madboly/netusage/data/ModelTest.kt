package com.madboly.netusage.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test
import java.time.LocalDate

class ModelTest {
    private fun d(s: String) = LocalDate.parse(s)

    @Test fun cycleStartsOnItsDay() {
        val c = Period.cycleOf(d("2026-09-29"), 27)
        assertEquals(d("2026-09-27"), c.start)
        assertEquals(d("2026-10-26"), c.end)
    }

    @Test fun cycleBeforeItsDayBelongsToLastMonth() {
        val c = Period.cycleOf(d("2026-09-20"), 27)
        assertEquals(d("2026-08-27"), c.start)
        assertEquals(d("2026-09-26"), c.end)
    }

    @Test fun cycleDayClampsInShortMonths() {
        val c = Period.cycleOf(d("2026-02-28"), 31)
        assertEquals(d("2026-02-28"), c.start)
        assertEquals(d("2026-03-30"), c.end)
    }

    @Test fun steppingACycleBackLandsInThePreviousOne() {
        val c = Period.cycleOf(d("2026-09-29"), 27)
        assertEquals(d("2026-08-27"), Period.cycleOf(step(c, -1), 27).start)
    }

    @Test fun weekIsTheSevenDaysEndingOnTheAnchor() {
        val w = Period.of(PeriodKind.WEEK, d("2026-09-29"), 1, d("2026-01-01"))
        assertEquals(d("2026-09-23"), w.start)
        assertEquals(7L, w.days)
    }

    @Test fun scopesRoundTrip() {
        for (s in listOf(Scope.All, Scope.Type(WIFI), Scope.Type(MOBILE), Scope.Named(WIFI, "ap:32:b1:42:ba"))) {
            assertEquals(s, Scope.decode(s.encode()))
        }
    }

    @Test fun namedScopesIgnoreTheirTitle() {
        assertEquals(Scope.Named(WIFI, "ap:x", "Home"), Scope.Named(WIFI, "ap:x"))
        assertNotEquals(Scope.Named(WIFI, "ap:x"), Scope.Named(MOBILE, "ap:x"))
    }

    @Test fun humanUses1024Units() {
        assertEquals("1.46 GB", human((1.46 * (1L shl 30)).toLong()))
        assertEquals("512 B", human(512))
    }
}
