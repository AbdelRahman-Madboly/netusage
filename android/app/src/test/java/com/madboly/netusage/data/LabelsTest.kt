package com.madboly.netusage.data

import org.junit.Assert.assertEquals
import org.junit.Test

class LabelsTest {
    @Test fun networkLabelUsesTheNameAfterTheKeyPrefix() {
        assertEquals("Home", networkLabel(WIFI, "ssid:Home"))
        assertEquals("a:b", networkLabel(WIFI, "ap:a:b"))
    }

    @Test fun networkLabelWithoutAKeySaysWhichKindIsUnknown() {
        assertEquals("Unknown Wi-Fi", networkLabel(WIFI, ""))
        assertEquals("Unknown wired", networkLabel(WIRED, ""))
        assertEquals("Unknown SIM", networkLabel("sim", ""))
    }

    @Test fun humanDropsDecimalsAsValuesGrow() {
        val gb = 1L shl 30
        assertEquals("1.50 GB", human(gb * 3 / 2))
        assertEquals("12.5 GB", human(gb * 25 / 2))
        assertEquals("250 GB", human(gb * 250))
        assertEquals("1.00 KB", human(1024))
        assertEquals("2.00 TB", human(2L shl 40))
    }

    @Test fun gbToBytesIsBinary() {
        assertEquals(1L shl 30, gbToBytes(1.0))
        assertEquals(5L shl 29, gbToBytes(2.5))
    }

    @Test fun totalsAddFieldByField() {
        val t = Totals(1, 2, 3) + Totals(10, 20, 30)
        assertEquals(Totals(11, 22, 33), t)
        assertEquals(66, t.total)
    }
}
