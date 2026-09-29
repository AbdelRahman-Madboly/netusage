package com.madboly.netusage

import android.Manifest
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.DateRange
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import com.madboly.netusage.data.MOBILE
import com.madboly.netusage.data.Period
import com.madboly.netusage.data.PeriodKind
import com.madboly.netusage.data.Prefs
import com.madboly.netusage.data.Repo
import com.madboly.netusage.data.Scope
import com.madboly.netusage.data.Sync
import com.madboly.netusage.data.Tracker
import com.madboly.netusage.data.WIFI
import com.madboly.netusage.data.WIRED
import com.madboly.netusage.ui.AppState
import com.madboly.netusage.ui.AppsScreen
import com.madboly.netusage.ui.HistoryScreen
import com.madboly.netusage.ui.NetUsageTheme
import com.madboly.netusage.ui.NetworksScreen
import com.madboly.netusage.ui.OverviewScreen
import com.madboly.netusage.ui.SettingsScreen
import com.madboly.netusage.ui.load
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.LocalDate

class MainActivity : ComponentActivity() {
    private val askPermissions = registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        Tracker.start(this)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        askPermissions.launch(
            arrayOf(
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.READ_PHONE_STATE,
                Manifest.permission.POST_NOTIFICATIONS,
            )
        )
        Tracker.start(this)
        setContent { NetUsageTheme { Root() } }
    }
}

private enum class Tab(val label: String) { OVERVIEW("Overview"), NETWORKS("Networks"), APPS("Apps"), HISTORY("History"), SETTINGS("Settings") }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Root() {
    val context = LocalContext.current
    val state = remember {
        val prefs = Prefs(context)
        AppState(Repo(context, prefs.devicesMode), prefs, Repo(context, "all"))
    }
    var tab by rememberSaveable { mutableStateOf(Tab.OVERVIEW) }
    var syncing by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    val lifecycle = LocalLifecycleOwner.current

    fun sync() {
        if (syncing) return
        syncing = true
        scope.launch {
            withContext(Dispatchers.IO) {
                Sync.run(context)
                state.firstDay = state.repo.firstDay()
            }
            syncing = false
            state.version++
        }
    }

    // Sync when opened, then keep the numbers fresh while the app is on screen.
    LaunchedEffect(Unit) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            while (true) {
                sync()
                delay(60_000)
            }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { ScopePicker(state) },
                actions = {
                    if (syncing) CircularProgressIndicator(Modifier.padding(12.dp).size(22.dp), strokeWidth = 2.dp)
                    else IconButton(onClick = ::sync) { Icon(Icons.Default.Refresh, "Sync now") }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.background),
            )
        },
        bottomBar = {
            NavigationBar {
                for (t in Tab.entries) {
                    NavigationBarItem(
                        selected = tab == t, onClick = { tab = t },
                        icon = {
                            when (t) {
                                Tab.OVERVIEW -> Icon(Icons.Default.Home, null)
                                Tab.NETWORKS -> Icon(painterResource(R.drawable.ic_wifi), null)
                                Tab.APPS -> Icon(Icons.Default.List, null)
                                Tab.HISTORY -> Icon(Icons.Default.DateRange, null)
                                Tab.SETTINGS -> Icon(Icons.Default.Settings, null)
                            }
                        },
                        label = { Text(t.label, maxLines = 1) },
                    )
                }
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when (tab) {
                Tab.OVERVIEW -> OverviewScreen(state, openNetworks = { tab = Tab.NETWORKS }, openApps = { tab = Tab.APPS })
                Tab.NETWORKS -> NetworksScreen(state, onPick = { tab = Tab.OVERVIEW })
                Tab.APPS -> AppsScreen(state)
                Tab.HISTORY -> HistoryScreen(state, openDay = { tab = Tab.OVERVIEW })
                Tab.SETTINGS -> SettingsScreen(state, onSync = ::sync)
            }
        }
    }
}

/** The title doubles as the network filter: all traffic, a type, or one router or SIM. */
@Composable
private fun ScopePicker(state: AppState) {
    var open by remember { mutableStateOf(false) }
    val groups = load(emptyList(), state.version) {
        state.repo.networks(Period(PeriodKind.ALL, state.repo.firstDay(), LocalDate.now()))
    }
    val label = load(state.scope.label, state.scope, state.version) { state.repo.labelFor(state.scope) }
    Box {
        TextButton(onClick = { open = true }) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    painterResource(if (state.scope.net == MOBILE) R.drawable.ic_sim else R.drawable.ic_wifi),
                    null, Modifier.size(20.dp)
                )
                Text("  $label", style = MaterialTheme.typography.titleLarge, maxLines = 1,
                    overflow = TextOverflow.Ellipsis, modifier = Modifier.widthIn(max = 230.dp))
                Icon(Icons.Default.ArrowDropDown, null)
            }
        }
        DropdownMenu(expanded = open, onDismissRequest = { open = false }) {
            @Composable
            fun item(s: Scope, text: String, icon: ImageVector? = null) = DropdownMenuItem(
                text = { Text(text) },
                leadingIcon = { if (s == state.scope) Icon(Icons.Default.Check, null) else icon?.let { Icon(it, null) } },
                onClick = { state.select(s); open = false },
            )
            if (state.prefs.laptopToken.isNotEmpty() && !state.prefs.isMember) {
                for ((mode, text) in listOf("all" to "All devices", "mine" to "My phone + laptop", "phone" to "This phone only")) {
                    DropdownMenuItem(
                        text = { Text(text) },
                        leadingIcon = { if (state.devicesMode == mode) Icon(Icons.Default.Check, null) },
                        onClick = { state.showDevices(mode); open = false },
                    )
                }
                HorizontalDivider()
            }
            item(Scope.All, "All networks")
            item(Scope.Type(WIFI), "All Wi-Fi")
            item(Scope.Type(MOBILE), "All mobile")
            val wifi = groups.filter { it.net == WIFI }
            val wired = groups.filter { it.net == WIRED }
            val mobile = groups.filter { it.net == MOBILE }
            if (wifi.isNotEmpty()) HorizontalDivider()
            for (g in wifi) item(Scope.Named(WIFI, g.key, g.label), g.label)
            if (mobile.isNotEmpty()) HorizontalDivider()
            for (g in mobile) item(Scope.Named(MOBILE, g.key, g.label), g.label)
            if (wired.isNotEmpty()) HorizontalDivider()
            for (g in wired) item(Scope.Named(WIRED, g.key, g.label), g.label)
        }
    }
}
