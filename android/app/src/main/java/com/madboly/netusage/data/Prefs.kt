package com.madboly.netusage.data

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.madboly.netusage.MainActivity
import com.madboly.netusage.NetUsageApp
import com.madboly.netusage.R
import org.json.JSONObject
import java.time.LocalDate

/** Limits and plan, the phone's counterpart of the laptop's limits.conf. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("netusage", Context.MODE_PRIVATE)

    var cycleDay: Int
        get() = sp.getInt("cycle_day", 1)
        set(v) = sp.edit().putInt("cycle_day", v.coerceIn(1, 31)).apply()

    /** Data plan size in GB for the billing cycle, 0 for none. */
    var planGb: Double
        get() = sp.getFloat("plan_gb", 0f).toDouble()
        set(v) = sp.edit().putFloat("plan_gb", v.toFloat()).apply()

    var planScope: Scope
        get() = Scope.decode(sp.getString("plan_scope", MOBILE))
        set(v) = sp.edit().putString("plan_scope", v.encode()).apply()

    var dailyGb: Double
        get() = sp.getFloat("daily_gb", 0f).toDouble()
        set(v) = sp.edit().putFloat("daily_gb", v.toFloat()).apply()

    var dailyScope: Scope
        get() = Scope.decode(sp.getString("daily_scope", MOBILE))
        set(v) = sp.edit().putString("daily_scope", v.encode()).apply()

    var dailyPercents: Set<Int>
        get() = sp.getStringSet("daily_pct", setOf("80", "100"))!!.map { it.toInt() }.toSet()
        set(v) = sp.edit().putStringSet("daily_pct", v.map { it.toString() }.toSet()).apply()

    var planPercents: Set<Int>
        get() = sp.getStringSet("plan_pct", setOf("75", "90", "100"))!!.map { it.toInt() }.toSet()
        set(v) = sp.edit().putStringSet("plan_pct", v.map { it.toString() }.toSet()).apply()

    /** Minutes between reminders once past a limit, 0 for no reminders. */
    var repeatMinutes: Int
        get() = sp.getInt("repeat_min", 0)
        set(v) = sp.edit().putInt("repeat_min", v).apply()

    /** Laptop address on the home network; found again automatically if it changes. */
    var laptopHost: String
        get() = sp.getString("laptop_host", "")!!
        set(v) = sp.edit().putString("laptop_host", v.trim()).apply()

    /** Contents of the laptop's /var/lib/netusage/share.token. */
    var laptopToken: String
        get() = sp.getString("laptop_token", "")!!
        set(v) = sp.edit().putString("laptop_token", v.trim()).apply()

    /** "master" (my phone: sees every device, sets the plan) or "member" (a family phone). */
    val isMember: Boolean get() = sp.getString("role", "master") == "member"

    /** A family phone's id and name, as registered on the laptop. */
    val memberId: String get() = sp.getString("member_id", "")!!
    val deviceName: String get() = sp.getString("device_name", "")!!

    /** What screens add to this phone's own usage: "all", "mine" (phone + laptop) or "phone". */
    var devicesMode: String
        get() = if (isMember) "phone" else sp.getString("devices_mode", "all")!!
        set(v) = sp.edit().putString("devices_mode", v).apply()

    /** Extra bundle bought this cycle, in GB, and the cycle (its start date) it was for. */
    var extraGb: Double
        get() = sp.getFloat("extra_gb", 0f).toDouble()
        set(v) = sp.edit().putFloat("extra_gb", v.toFloat()).apply()

    var extraCycle: String
        get() = sp.getString("extra_cycle", "")!!
        set(v) = sp.edit().putString("extra_cycle", v).apply()

    /** The bundle as the laptop last reported it, for a family phone: JSON or "". */
    var memberPlan: String
        get() = sp.getString("member_plan", "")!!
        set(v) = sp.edit().putString("member_plan", v).apply()

    /** Plan usage the ISP reported (GB used this cycle) and when it was entered. */
    var ispUsedGb: Double
        get() = sp.getFloat("isp_used_gb", 0f).toDouble()
        set(v) = sp.edit().putFloat("isp_used_gb", v.toFloat()).apply()

    var ispAt: Long
        get() = sp.getLong("isp_at", 0)
        set(v) = sp.edit().putLong("isp_at", v).apply()

    /** What the app shows, remembered between launches. */
    var viewScope: Scope
        get() = Scope.decode(sp.getString("view_scope", "all"))
        set(v) = sp.edit().putString("view_scope", v.encode()).apply()

    /**
     * assets/config.json carries settings chosen when the app was built for this phone.
     * Each new "version" is applied once, so an update can change the plan without
     * clearing the app, and later edits in Settings are not overwritten.
     */
    fun applyBundled(context: Context) {
        val json = runCatching {
            JSONObject(context.assets.open("config.json").bufferedReader().readText())
        }.getOrNull() ?: return
        val version = json.optInt("version")
        if (version <= sp.getInt("config_version", 0)) return
        applyImported(json)
        if (json.has("view_scope")) sp.edit().putString("view_scope", json.getString("view_scope")).apply()
        // Fetch the laptop's whole history again, e.g. after the laptop starts sending more.
        if (json.optBoolean("laptop_resync")) Db.get(context).setMeta("laptop_synced_to", "0")
        sp.edit().putInt("config_version", version).apply()
    }

    fun applyImported(json: JSONObject) {
        val e = sp.edit()
        if (json.has("cycle_day")) e.putInt("cycle_day", json.getInt("cycle_day"))
        if (json.has("plan_gb")) e.putFloat("plan_gb", json.getDouble("plan_gb").toFloat())
        if (json.has("plan_scope")) e.putString("plan_scope", json.getString("plan_scope"))
        if (json.has("daily_gb")) e.putFloat("daily_gb", json.getDouble("daily_gb").toFloat())
        if (json.has("isp_used_gb")) e.putFloat("isp_used_gb", json.getDouble("isp_used_gb").toFloat())
        if (json.has("isp_at")) e.putLong("isp_at", json.getLong("isp_at"))
        if (json.has("laptop_host")) e.putString("laptop_host", json.getString("laptop_host"))
        if (json.has("role")) e.putString("role", json.getString("role"))
        if (json.has("member_id")) e.putString("member_id", json.getString("member_id"))
        if (json.has("device_name")) e.putString("device_name", json.getString("device_name"))
        if (json.has("laptop_token")) e.putString("laptop_token", json.getString("laptop_token"))
        e.apply()
    }

    companion object {
        val DAILY_CHOICES = listOf(50, 80, 90, 100, 150, 200)
        val PLAN_CHOICES = listOf(50, 75, 90, 100)
        val REPEAT_CHOICES = listOf(0, 15, 30, 60, 120, 240)
    }
}

object Alerts {
    fun check(context: Context) {
        val prefs = Prefs(context)
        val repo = Repo(context, devicesMode = "all")
        val db = Db.get(context)
        val today = LocalDate.now()
        val now = System.currentTimeMillis() / 1000

        if (prefs.dailyGb > 0) {
            val limit = gbToBytes(prefs.dailyGb)
            val used = repo.totals(prefs.dailyScope, Period(PeriodKind.DAY, today, today)).total
            fire(context, db, "day:$today", used, limit, prefs.dailyPercents, prefs.repeatMinutes, now, 2) { pct ->
                "Today: ${human(used)} of ${human(limit)} daily limit ($pct%)" to prefs.dailyScope.let { repo.labelFor(it) }
            }
        }
        planStatus(prefs, repo)?.let { st ->
            val limit = st.plan
            val cycle = st.cycle
            val used = st.used
            fire(context, db, "cycle:${cycle.start}", used, limit, prefs.planPercents, prefs.repeatMinutes, now, 3) { pct ->
                "Plan: ${human(used)} of ${human(limit)} this cycle ($pct%)" to
                    "${repo.labelFor(prefs.planScope)} · resets ${cycle.end.plusDays(1)}"
            }
        }
    }

    private fun fire(
        context: Context, db: Db, tag: String, used: Long, limit: Long, percents: Set<Int>,
        repeatMin: Int, now: Long, id: Int, text: (Int) -> Pair<String, String>,
    ) {
        val pct = (used * 100 / limit).toInt()
        val crossed = percents.filter { pct >= it }.maxOrNull() ?: return
        val key = "alert:$tag:$crossed"
        val last = db.meta(key)?.toLongOrNull()
        val due = last == null || (crossed >= 100 && repeatMin > 0 && now - last >= repeatMin * 60L)
        if (!due) return
        db.setMeta(key, now.toString())
        val (title, body) = text(pct)
        notify(context, id, title, body)
    }

    fun notify(context: Context, id: Int, title: String, body: String) {
        // before Android 13 there is no permission, only the switch in system settings
        val allowed = if (android.os.Build.VERSION.SDK_INT >= 33)
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        else NotificationManagerCompat.from(context).areNotificationsEnabled()
        if (!allowed) return
        val open = PendingIntent.getActivity(context, 0, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val n = NotificationCompat.Builder(context, NetUsageApp.CHANNEL_ALERTS)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(body)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(id, n)
    }
}
