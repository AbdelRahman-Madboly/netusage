package com.madboly.netusage

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.madboly.netusage.data.Sync
import com.madboly.netusage.data.Tracker
import java.util.concurrent.TimeUnit

class NetUsageApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_TRACKER, "Tracking", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shown while Net Usage records which network is in use"
            }
        )
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ALERTS, "Limits", NotificationManager.IMPORTANCE_HIGH).apply {
                description = "Daily limit and data plan warnings"
            }
        )
        WorkManager.getInstance(this).enqueueUniquePeriodicWork(
            "sync", ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<SyncWorker>(30, TimeUnit.MINUTES).build()
        )
    }

    companion object {
        const val CHANNEL_TRACKER = "tracker"
        const val CHANNEL_ALERTS = "alerts"
    }
}

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        Sync.run(applicationContext)
        return Result.success()
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED || intent.action == Intent.ACTION_MY_PACKAGE_REPLACED) {
            Tracker.start(context)
        }
    }
}
