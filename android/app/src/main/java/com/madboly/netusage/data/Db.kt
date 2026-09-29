package com.madboly.netusage.data

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * Local store. Android only keeps about three months of network history, so everything
 * read from it is copied here and kept for good.
 *
 * networks   one row per network key. Wi-Fi seen by the tracker is keyed by router
 *            ("ap:" + the middle of its BSSID) so renaming the SSID or changing the
 *            password keeps the same key; label follows the latest SSID. Imported history
 *            only knew SSIDs ("ssid:Name"). grp points every key at the one it is shown
 *            under, which is how bands of one router and renamed networks are merged.
 * usage      hourly bytes per network key (Wi-Fi router or SIM, "" when unknown).
 *            split = 0 marks rows imported from My Data Manager, which only knew the total
 *            (stored in rx, tx = 0).
 * app_usage  daily bytes per app per network type.
 * remote_usage  hourly bytes other devices (the laptop) report, per network key; the
 *            keys are the same, so a router both use shows up as one network.
 * samples    the tracker's own byte counts per hour and named network; used only to share
 *            each hour's system total out between the networks that were connected.
 */
class Db private constructor(context: Context) :
    SQLiteOpenHelper(context, "netusage.db", null, 2) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE usage (
                hour INTEGER NOT NULL, net TEXT NOT NULL, name TEXT NOT NULL,
                rx INTEGER NOT NULL, tx INTEGER NOT NULL,
                split INTEGER NOT NULL DEFAULT 1, src TEXT NOT NULL,
                PRIMARY KEY (hour, net, name))"""
        )
        db.execSQL(
            """CREATE TABLE app_usage (
                day TEXT NOT NULL, net TEXT NOT NULL, app TEXT NOT NULL,
                rx INTEGER NOT NULL, tx INTEGER NOT NULL,
                PRIMARY KEY (day, net, app))"""
        )
        db.execSQL(
            """CREATE TABLE samples (
                hour INTEGER NOT NULL, net TEXT NOT NULL, name TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                PRIMARY KEY (hour, net, name))"""
        )
        db.execSQL(
            """CREATE TABLE networks (
                key TEXT PRIMARY KEY, net TEXT NOT NULL, label TEXT NOT NULL,
                grp TEXT NOT NULL, names TEXT NOT NULL DEFAULT '', last_seen INTEGER NOT NULL DEFAULT 0)"""
        )
        db.execSQL("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        createRemote(db)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        if (oldVersion < 2) createRemote(db)
    }

    private fun createRemote(db: SQLiteDatabase) {
        db.execSQL(
            """CREATE TABLE IF NOT EXISTS remote_usage (
                device TEXT NOT NULL, hour INTEGER NOT NULL, net TEXT NOT NULL, name TEXT NOT NULL,
                rx INTEGER NOT NULL, tx INTEGER NOT NULL,
                PRIMARY KEY (device, hour, net, name))"""
        )
    }

    fun meta(key: String): String? =
        readableDatabase.rawQuery("SELECT value FROM meta WHERE key = ?", arrayOf(key)).use {
            if (it.moveToFirst()) it.getString(0) else null
        }

    fun setMeta(key: String, value: String) {
        writableDatabase.insertWithOnConflict(
            "meta", null,
            ContentValues().apply { put("key", key); put("value", value) },
            SQLiteDatabase.CONFLICT_REPLACE
        )
    }

    fun addSample(hour: Long, net: String, name: String, bytes: Long) {
        // Two steps rather than an upsert: Android 9's SQLite predates ON CONFLICT DO UPDATE.
        val w = writableDatabase
        w.execSQL("INSERT OR IGNORE INTO samples (hour, net, name, bytes) VALUES (?, ?, ?, 0)", arrayOf(hour, net, name))
        w.execSQL("UPDATE samples SET bytes = bytes + ? WHERE hour = ? AND net = ? AND name = ?", arrayOf(bytes, hour, net, name))
    }

    fun samples(hour: Long, net: String): Map<String, Long> =
        readableDatabase.rawQuery(
            "SELECT name, bytes FROM samples WHERE hour = ? AND net = ?",
            arrayOf(hour.toString(), net)
        ).use { c ->
            buildMap { while (c.moveToNext()) put(c.getString(0), c.getLong(1)) }
        }

    /** Hours for this network type that came from the one-off import and must not be rewritten. */
    fun importedHours(net: String, from: Long): Set<Long> =
        readableDatabase.rawQuery(
            "SELECT DISTINCT hour FROM usage WHERE src = 'import' AND net = ? AND hour >= ?",
            arrayOf(net, from.toString())
        ).use { c -> buildSet { while (c.moveToNext()) add(c.getLong(0)) } }

    companion object {
        @Volatile private var instance: Db? = null
        fun get(context: Context): Db =
            instance ?: synchronized(this) {
                instance ?: Db(context.applicationContext).also { instance = it }
            }
    }
}
