package com.madboly.netusage.data

import android.annotation.SuppressLint
import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.TrafficStats
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.telephony.SubscriptionManager
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.madboly.netusage.MainActivity
import com.madboly.netusage.NetUsageApp
import com.madboly.netusage.R
import java.time.LocalDate

/**
 * Android's history says how much went over Wi-Fi and mobile, but not which router or
 * which SIM. This service watches that: every minute, and whenever the connection
 * changes, it adds the bytes moved since the last look to whatever network was connected.
 * The hourly sync then uses those shares to split the system's exact totals.
 */
class Tracker : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private lateinit var repo: Repo
    private lateinit var db: Db

    private var lastMobile = -1L
    private var lastTotal = -1L
    private var wifiKey = ""
    private var mobileKey = ""
    private var ticks = 0

    private val tickRunnable = object : Runnable {
        override fun run() {
            tick()
            handler.postDelayed(this, 60_000)
        }
    }

    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = post()
        override fun onLost(network: Network) = post()
        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = post()
        private fun post() { handler.post { tick() } }
    }

    override fun onCreate() {
        super.onCreate()
        db = Db.get(this)
        repo = Repo(this)
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, notification("Starting"),
            ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
        )
        val cm = getSystemService(ConnectivityManager::class.java)
        cm.registerNetworkCallback(NetworkRequest.Builder().build(), callback)
        handler.post(tickRunnable)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int) = START_STICKY

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(callback) }
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun tick() {
        val now = System.currentTimeMillis() / 1000
        val mobile = TrafficStats.getMobileRxBytes() + TrafficStats.getMobileTxBytes()
        val total = TrafficStats.getTotalRxBytes() + TrafficStats.getTotalTxBytes()
        if (lastTotal >= 0 && total >= lastTotal && mobile >= lastMobile) {
            val dMobile = mobile - lastMobile
            val dWifi = (total - lastTotal) - dMobile
            val hour = now - now % 3600
            // Bytes since the last look went over whatever was connected at the last look.
            if (dMobile > 0) db.addSample(hour, MOBILE, mobileKey, dMobile)
            if (dWifi > 0 && wifiKey.isNotEmpty()) db.addSample(hour, WIFI, wifiKey, dWifi)
        }
        lastMobile = mobile
        lastTotal = total

        val wifi = currentWifi(this)
        wifiKey = wifi?.first ?: ""
        if (wifi != null) repo.seeNetwork(wifi.first, WIFI, wifi.second, now)
        val sim = currentSim(this)
        mobileKey = sim?.first ?: ""
        if (sim != null) repo.seeNetwork(sim.first, MOBILE, sim.second, now)
        db.setMeta("last_sample", now.toString())
        if (wifi != null) db.setMeta("cur:$WIFI", wifi.first)
        if (sim != null) db.setMeta("cur:$MOBILE", sim.first)

        if (ticks++ % 5 == 0) updateNotification(wifi?.second, sim?.second)
    }

    private fun updateNotification(wifi: String?, sim: String?) {
        val today = Period(PeriodKind.DAY, LocalDate.now(), LocalDate.now())
        val used = repo.totals(Scope.All, today).total
        val on = wifi?.let { "Wi-Fi: $it" } ?: sim?.let { "Mobile: $it" } ?: "Offline"
        val nm = getSystemService(android.app.NotificationManager::class.java)
        nm.notify(NOTIFICATION_ID, notification("$on · today ${human(used)}"))
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, NetUsageApp.CHANNEL_TRACKER)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Net Usage")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .build()
    }

    companion object {
        const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            runCatching {
                ContextCompat.startForegroundService(context, Intent(context, Tracker::class.java))
            }
        }

        /**
         * The router's key and the SSID it currently broadcasts. The key drops the first
         * and last octets of the BSSID: those are the parts that differ between a router's
         * 2.4 GHz and 5 GHz radios, while renaming or re-keying the network changes none of it.
         */
        @Suppress("DEPRECATION")
        @SuppressLint("MissingPermission")
        fun currentWifi(context: Context): Pair<String, String>? {
            val cm = context.getSystemService(ConnectivityManager::class.java)
            val caps = cm.getNetworkCapabilities(cm.activeNetwork)
            val wm = context.applicationContext.getSystemService(WifiManager::class.java)
            val info = wm.connectionInfo ?: return null
            if (info.networkId == -1 && caps?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) != true) return null
            val ssid = info.ssid?.removeSurrounding("\"")?.takeIf { it != WifiManager.UNKNOWN_SSID && it != "<unknown ssid>" } ?: ""
            val bssid = info.bssid?.takeIf { it != "02:00:00:00:00:00" }
            val key = when {
                bssid != null -> "ap:" + bssid.lowercase().split(':').drop(1).dropLast(1).joinToString(":")
                ssid.isNotEmpty() -> "ssid:$ssid"
                else -> return null
            }
            return key to ssid
        }

        @SuppressLint("MissingPermission")
        fun currentSim(context: Context): Pair<String, String>? = runCatching {
            val id = if (android.os.Build.VERSION.SDK_INT >= 30) SubscriptionManager.getActiveDataSubscriptionId()
            else SubscriptionManager.getDefaultDataSubscriptionId()
            if (id == SubscriptionManager.INVALID_SUBSCRIPTION_ID) return null
            val sm = context.getSystemService(SubscriptionManager::class.java)
            val info = sm.getActiveSubscriptionInfo(id) ?: return null
            val name = info.displayName?.toString()?.trim()?.replaceFirstChar { it.uppercase() }
                ?.ifEmpty { null } ?: "SIM ${info.simSlotIndex + 1}"
            "sim:$name" to name
        }.getOrNull()
    }
}
