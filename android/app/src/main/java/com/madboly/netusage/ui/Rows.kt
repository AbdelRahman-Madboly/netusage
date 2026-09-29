package com.madboly.netusage.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.graphics.drawable.toBitmap
import com.madboly.netusage.R
import com.madboly.netusage.data.AppNames
import com.madboly.netusage.data.AppRow
import com.madboly.netusage.data.MOBILE
import com.madboly.netusage.data.NetworkRow
import com.madboly.netusage.data.WIRED
import com.madboly.netusage.data.human

@Composable
fun NetworkLine(n: NetworkRow, max: Long, onClick: () -> Unit) {
    val color = if (n.net == MOBILE) MobileColor else WifiColor
    Column(Modifier.fillMaxWidth().clickable(onClick = onClick).padding(vertical = 6.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(painterResource(when (n.net) {
                MOBILE -> R.drawable.ic_sim
                WIRED -> R.drawable.ic_router
                else -> R.drawable.ic_wifi
            }), null, Modifier.size(18.dp), tint = color)
            Spacer(Modifier.width(8.dp))
            Text(n.label, Modifier.weight(1f), maxLines = 1)
            Text(human(n.totals.total), fontWeight = FontWeight.SemiBold)
        }
        Spacer(Modifier.height(5.dp))
        ShareBar(n.totals.total.toFloat() / max, color)
    }
}

@Composable
fun AppLine(a: AppRow, max: Long, label: String, onClick: (() -> Unit)? = null) {
    val context = LocalContext.current
    val icon = load<android.graphics.drawable.Drawable?>(null, a.app) { AppNames.icon(context, a.app) }
    Column(
        Modifier.fillMaxWidth().let { if (onClick != null) it.clickable(onClick = onClick) else it }.padding(vertical = 6.dp)
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            AppIcon(icon)
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text(label, maxLines = 1)
                Spacer(Modifier.height(4.dp))
                ShareBar(a.totals.total.toFloat() / max, Teal)
            }
            Spacer(Modifier.width(10.dp))
            Text(human(a.totals.total), fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
fun AppIcon(icon: android.graphics.drawable.Drawable?) {
    if (icon != null) {
        val bitmap = remember(icon) { icon.toBitmap(96, 96).asImageBitmap() }
        Image(bitmap, null, Modifier.size(32.dp))
    } else {
        Icon(Icons.Default.Settings, null, Modifier.size(32.dp).padding(4.dp),
            tint = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
