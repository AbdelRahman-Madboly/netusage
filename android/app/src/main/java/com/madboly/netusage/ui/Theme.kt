package com.madboly.netusage.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

// Brand colours come from the icon: the teal tile, the mint gauge and the amber upload arrow.
val Teal = Color(0xFF1596B3)
val TealDeep = Color(0xFF0A4D63)
val Mint = Color(0xFF3DD6A0)

val Down = Color(0xFF1596B3)
val Up = Color(0xFFF2A93B)
val Unsplit = Color(0xFF8E9BA1)
val Ok = Color(0xFF2EC27E)
val Warn = Color(0xFFE66100)
val Danger = Color(0xFFE01B24)
val WifiColor = Color(0xFF7C5CD6)
val MobileColor = Color(0xFF2EC27E)

private val Light = lightColorScheme(
    primary = Color(0xFF0E7C96),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFCDEEF6),
    onPrimaryContainer = TealDeep,
    secondary = Color(0xFF00896A),
    secondaryContainer = Color(0xFFCFF5E6),
    background = Color(0xFFF4F8FA),
    surface = Color(0xFFF4F8FA),
    surfaceContainer = Color(0xFFFFFFFF),
    surfaceContainerHigh = Color(0xFFE8F0F3),
    surfaceContainerLow = Color(0xFFFFFFFF),
)

private val Dark = darkColorScheme(
    primary = Color(0xFF6ED3EB),
    onPrimary = Color(0xFF00363F),
    primaryContainer = Color(0xFF0A4D63),
    onPrimaryContainer = Color(0xFFCDEEF6),
    secondary = Mint,
    secondaryContainer = Color(0xFF004F3B),
    background = Color(0xFF0E1719),
    surface = Color(0xFF0E1719),
    surfaceContainer = Color(0xFF172326),
    surfaceContainerHigh = Color(0xFF1F2E32),
    surfaceContainerLow = Color(0xFF172326),
)

@Composable
fun NetUsageTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, content = content)
}
