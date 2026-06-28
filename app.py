#!/usr/bin/env python3
"""
Server Monitor Dashboard — Backend
Collects system metrics and serves them via REST API.
"""

import os
import time
import json
import sqlite3
import socket
import threading
from datetime import datetime, timedelta
import multiprocessing
import asyncio
from contextlib import contextmanager, asynccontextmanager

import platform
import re
import urllib.request as _urllib_req

import psutil
import uvicorn
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect, Depends, HTTPException, status, UploadFile, File, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, RedirectResponse
import asyncssh
from pydantic import BaseModel
from collections import deque
import logging

try:
    from browser_mgr import browser_mgr
except ImportError:
    browser_mgr = None

# ─── Configuration ───────────────────────────────────────────────────────────

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metrics.db")
COLLECT_INTERVAL = 60  # seconds
RETENTION_DAYS = 31
PORT = 8080
VERSION = "1.0.42"
UI_REFRESH_INTERVAL = 3

RANGE_MAP = {
    "1h":  (3600,        None),       # raw data
    "2h":  (7200,        None),       # raw data
    "6h":  (21600,       60),         # 1 min average
    "12h": (43200,       60),         # 1 min average
    "1d":  (86400,       120),        # 2 min average
    "2d":  (172800,      300),        # 5 min average
    "1w":  (604800,      900),        # 15 min average
    "1m":  (2592000,     3600),       # 1 hour average
}

# --- Feature Flags Config ---
def is_v2ray_installed() -> bool:
    db_paths = ["/etc/x-ui/x-ui.db", "/usr/local/x-ui/x-ui.db", "/opt/x-ui/x-ui.db"]
    for p in db_paths:
        if os.path.exists(p): return True
    return os.path.exists("/etc/default/x-ui")

ENABLE_V2RAY = os.environ.get("ENABLE_V2RAY", "true" if is_v2ray_installed() else "false").lower() in ("true", "1", "yes")


# ─── X-UI Dynamic Paths ──────────────────────────────────────────────────────
class XUIPaths:
    @staticmethod
    def get_db_path():
        paths = ["/etc/x-ui/x-ui.db", "/usr/local/x-ui/x-ui.db", "/opt/x-ui/x-ui.db"]
        for p in paths:
            if os.path.exists(p): return p
        return "/etc/x-ui/x-ui.db"
    
    @staticmethod
    def get_access_log():
        paths = ["/var/log/x-ui/access.log", "/usr/local/x-ui/access.log", "/var/log/xray/access.log", "/etc/x-ui/access.log"]
        for p in paths:
            if os.path.exists(p): return p
        return "/var/log/x-ui/access.log"

    @staticmethod
    def get_geosite_path():
        paths = ["/usr/local/x-ui/bin/geosite.dat", "/usr/bin/xray/geosite.dat", "/usr/local/bin/geosite.dat"]
        for p in paths:
            if os.path.exists(p): return p
        return "/usr/local/x-ui/bin/geosite.dat"

    @staticmethod
    def get_xray_config():
        paths = ["/usr/local/x-ui/bin/config.json", "/etc/x-ui/config.json", "/usr/local/etc/xray/config.json"]
        for p in paths:
            if os.path.exists(p): return p
        return "/usr/local/x-ui/bin/config.json"

# ─── Public IP Cache ─────────────────────────────────────────────────────────

_ip_cache: dict = {"ipv4": "—", "ipv6": "—", "at": 0.0}
_IP_TTL = 300  # refresh every 5 minutes

def _fetch_ip(url: str, timeout: int = 4) -> str:
    """Fetch a single URL and return the text body or empty string."""
    try:
        with _urllib_req.urlopen(url, timeout=timeout) as r:
            return r.read().decode().strip()
    except Exception:
        return ""

def get_public_ips() -> tuple[str, str]:
    """Return (public_ipv4, public_ipv6). Results are cached for _IP_TTL seconds."""
    global _ip_cache
    now = time.time()
    if now - _ip_cache["at"] < _IP_TTL:
        return _ip_cache["ipv4"], _ip_cache["ipv6"]

    ipv4 = _fetch_ip("https://api4.ipify.org") or "—"
    ipv6 = _fetch_ip("https://api6.ipify.org") or "—"

    _ip_cache = {"ipv4": ipv4, "ipv6": ipv6, "at": now}
    return ipv4, ipv6

# ─── Database ────────────────────────────────────────────────────────────────

class RowWrapper:
    def __init__(self, row_dict):
        self._dict = row_dict
    
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self._dict.values())[key]
        return self._dict[key]
        
    def keys(self):
        return list(self._dict.keys())

class PostgresCursorWrapper:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params=None):
        if params is not None:
            # Translate '?' to '%s' for PostgreSQL compatibility
            sql = sql.replace('?', '%s')
            self.cursor.execute(sql, params)
        else:
            self.cursor.execute(sql)
        return self

    def fetchall(self):
        desc = self.cursor.description
        if not desc:
            return []
        colnames = [d[0] for d in desc]
        rows = self.cursor.fetchall()
        return [RowWrapper(dict(zip(colnames, row))) for row in rows]

    def fetchone(self):
        row = self.cursor.fetchone()
        if not row:
            return None
        desc = self.cursor.description
        colnames = [d[0] for d in desc]
        return RowWrapper(dict(zip(colnames, row)))

class PostgresConnectionWrapper:
    def __init__(self, conn):
        self.conn = conn

    def cursor(self):
        return PostgresCursorWrapper(self.conn.cursor())

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

def init_db():
    """Initialize the database (PostgreSQL if MONITOR_DB_DSN is set, else SQLite)."""
    dsn = os.environ.get("MONITOR_DB_DSN")
    if dsn:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id SERIAL PRIMARY KEY,
                    timestamp DOUBLE PRECISION NOT NULL,
                    cpu_percent REAL NOT NULL,
                    ram_percent REAL NOT NULL,
                    ram_used_gb REAL NOT NULL,
                    ram_total_gb REAL NOT NULL,
                    disk_percent REAL NOT NULL,
                    disk_used_gb REAL NOT NULL,
                    disk_total_gb REAL NOT NULL,
                    net_sent_bytes DOUBLE PRECISION NOT NULL,
                    net_recv_bytes DOUBLE PRECISION NOT NULL,
                    net_sent_rate REAL NOT NULL DEFAULT 0,
                    net_recv_rate REAL NOT NULL DEFAULT 0,
                    conn_json TEXT DEFAULT '{}',
                    extra_json TEXT DEFAULT '{}'
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics(timestamp)")
    else:
        with get_db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    cpu_percent REAL NOT NULL,
                    ram_percent REAL NOT NULL,
                    ram_used_gb REAL NOT NULL,
                    ram_total_gb REAL NOT NULL,
                    disk_percent REAL NOT NULL,
                    disk_used_gb REAL NOT NULL,
                    disk_total_gb REAL NOT NULL,
                    net_sent_bytes REAL NOT NULL,
                    net_recv_bytes REAL NOT NULL,
                    net_sent_rate REAL NOT NULL DEFAULT 0,
                    net_recv_rate REAL NOT NULL DEFAULT 0,
                    conn_json TEXT DEFAULT '{}',
                    extra_json TEXT DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_metrics_timestamp
                ON metrics(timestamp)
            """)
            # Migration: add conn_json if missing
            try:
                conn.execute("SELECT conn_json FROM metrics LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE metrics ADD COLUMN conn_json TEXT DEFAULT '{}'")
            # Migration: add extra_json if missing
            try:
                conn.execute("SELECT extra_json FROM metrics LIMIT 1")
            except sqlite3.OperationalError:
                conn.execute("ALTER TABLE metrics ADD COLUMN extra_json TEXT DEFAULT '{}'")

    # Initialize Telegram alerts configuration table
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_config (
                id INTEGER PRIMARY KEY,
                bot_token TEXT DEFAULT '',
                chat_id TEXT DEFAULT '',
                interval_hours INTEGER DEFAULT 0,
                cpu_threshold REAL DEFAULT 0.0,
                ram_threshold REAL DEFAULT 0.0,
                load_threshold REAL DEFAULT 0.0,
                disk_threshold REAL DEFAULT 0.0,
                last_routine_sent REAL DEFAULT 0.0,
                send_sys_graph INTEGER DEFAULT 1,
                send_net_graph INTEGER DEFAULT 1,
                send_cpu_graph INTEGER DEFAULT 1,
                send_ram_graph INTEGER DEFAULT 1,
                send_load_graph INTEGER DEFAULT 1,
                send_load_1m_graph INTEGER DEFAULT 1,
                send_load_5m_graph INTEGER DEFAULT 1,
                send_load_15m_graph INTEGER DEFAULT 1
            )
        """)
        # Migrations for missing columns
        for col_name, col_type, default_val in [
            ('send_graph', 'INTEGER', 0),
            ('enabled', 'INTEGER', 1),
            ('graph_hours', 'INTEGER', 3),
            ('custom_interval_minutes', 'INTEGER', 0),
            ('load_avg_type', 'INTEGER', 1),
            ('alert_send_graph', 'INTEGER', 0),
            ('send_sys_graph', 'INTEGER', 1),
            ('send_net_graph', 'INTEGER', 1),
            ('send_cpu_graph', 'INTEGER', 1),
            ('send_ram_graph', 'INTEGER', 1),
            ('send_load_graph', 'INTEGER', 1),
            ('send_load_1m_graph', 'INTEGER', 1),
            ('send_load_5m_graph', 'INTEGER', 1),
            ('send_load_15m_graph', 'INTEGER', 1)
        ]:
            column_exists = False
            if dsn:
                try:
                    cur = conn.execute(f"""
                        SELECT 1 FROM information_schema.columns 
                        WHERE table_name='telegram_config' AND column_name='{col_name}'
                    """)
                    column_exists = cur.fetchone() is not None
                except Exception:
                    pass
            else:
                try:
                    cur = conn.execute("PRAGMA table_info(telegram_config)")
                    columns = [row[1] for row in cur.fetchall()]
                    column_exists = col_name in columns
                except Exception:
                    pass

            if not column_exists:
                try:
                    conn.execute(f"ALTER TABLE telegram_config ADD COLUMN {col_name} {col_type} DEFAULT {default_val}")
                except Exception as e:
                    print(f"[Migration Error] Failed to alter telegram_config ({col_name}): {e}")

        # Insert default settings if empty
        cur = conn.execute("SELECT COUNT(*) FROM telegram_config WHERE id = 1")
        if cur.fetchone()[0] == 0:
            conn.execute("""
                INSERT INTO telegram_config
                (id, bot_token, chat_id, interval_hours, cpu_threshold, ram_threshold, load_threshold, disk_threshold, last_routine_sent, send_graph, enabled, graph_hours, custom_interval_minutes, load_avg_type, alert_send_graph, send_sys_graph, send_net_graph, send_cpu_graph, send_ram_graph, send_load_graph, send_load_1m_graph, send_load_5m_graph, send_load_15m_graph)
                VALUES (1, '', '', 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 1, 3, 0, 1, 0, 1, 1, 1, 1, 1, 1, 1, 1)
            """)



@contextmanager
def get_db():
    """Context manager for database connections (SQLite or PostgreSQL)."""
    dsn = os.environ.get("MONITOR_DB_DSN")
    if dsn:
        from urllib.parse import urlparse
        params = {}
        if dsn.startswith("postgresql://") or dsn.startswith("postgres://"):
            url = urlparse(dsn)
            if url.username: params['user'] = url.username
            if url.password: params['password'] = url.password
            if url.hostname: params['host'] = url.hostname
            if url.port: params['port'] = int(url.port)
            if url.path: params['database'] = url.path.lstrip('/')
        else:
            for part in dsn.split():
                if '=' in part:
                    k, v = part.split('=', 1)
                    if k == 'dbname':
                        params['database'] = v
                    elif k == 'port':
                        params['port'] = int(v)
                    else:
                        params[k] = v
        
        pg_conn = None
        try:
            import pg8000
            pg_conn = pg8000.connect(**params)
        except Exception as e_pg8000:
            try:
                import psycopg2
                pg_conn = psycopg2.connect(dsn, connect_timeout=5)
            except Exception as e_psycopg2:
                raise RuntimeError(
                    f"Failed to connect to PostgreSQL. pg8000 error: {e_pg8000}; psycopg2 error: {e_psycopg2}"
                )
                
        wrapped_conn = PostgresConnectionWrapper(pg_conn)
        try:
            yield wrapped_conn
            wrapped_conn.commit()
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass
            raise
        finally:
            wrapped_conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def send_telegram_message(bot_token: str, chat_id: str, text: str):
    import urllib.request
    import urllib.parse
    import urllib.error
    import json
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({
                "keyboard": [
                    [{"text": "📊 Check Status"}]
                ],
                "resize_keyboard": True
            })
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        print(f"[Telegram Alert Error] HTTP Error {e.code}: {e.reason} - Body: {body}")
        raise e
    except Exception as e:
        print(f"[Telegram Alert Error] {e}")
        raise e

def generate_system_graph(image_path: str, hours: int = 3):
    import sqlite3
    import time
    import json
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime
    
    # Query last N hours
    now = time.time()
    cutoff_time = now - hours * 3600
    
    timestamps = []
    cpu_vals = []
    ram_vals = []
    load_1m_vals = []
    load_5m_vals = []
    load_15m_vals = []
    
    try:
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT timestamp, cpu_percent, ram_percent, extra_json 
                FROM metrics 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
            
            for row in cursor.fetchall():
                ts = row["timestamp"]
                timestamps.append(datetime.fromtimestamp(ts))
                cpu_vals.append(row["cpu_percent"])
                ram_vals.append(row["ram_percent"])
                
                load_1m = 0.0
                load_5m = 0.0
                load_15m = 0.0
                try:
                    extra = json.loads(row["extra_json"] or "{}")
                    if "load_avg" in extra and isinstance(extra["load_avg"], list):
                        l_avg = extra["load_avg"]
                        if len(l_avg) > 0: load_1m = l_avg[0]
                        if len(l_avg) > 1: load_5m = l_avg[1]
                        if len(l_avg) > 2: load_15m = l_avg[2]
                except Exception:
                    pass
                load_1m_vals.append(load_1m)
                load_5m_vals.append(load_5m)
                load_15m_vals.append(load_15m)
    except Exception as dbe:
        print(f"[System Graph DB Error] {dbe}")
            
    if not timestamps:
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No metrics collected yet", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()
        return

    try:
        plt.style.use('dark_background')
        fig, ax1 = plt.subplots(figsize=(10, 5.2), dpi=200)
        fig.patch.set_facecolor('#0f172a') # Slate 900
        ax1.set_facecolor('#1e293b') # Slate 800
        
        plt.title(f"System Resources - Last {hours} Hours", fontsize=12, fontweight='bold', pad=12, color='#f8fafc')
        
        # Grid
        ax1.grid(True, linestyle='--', color='#334155', alpha=0.5)
        
        # Plot CPU & RAM on left y-axis
        line_cpu, = ax1.plot(timestamps, cpu_vals, label="CPU (%)", color="#3b82f6", linewidth=1.5)
        line_ram, = ax1.plot(timestamps, ram_vals, label="RAM (%)", color="#10b981", linewidth=1.5)
        ax1.set_ylabel("Usage (%)", color="#94a3b8", fontsize=9)
        ax1.set_ylim(-2, 102)
        ax1.tick_params(axis='y', labelcolor="#94a3b8", labelsize=8)
        
        # Plot Load Average on right y-axis
        ax2 = ax1.twinx()
        line_load_1m, = ax2.plot(timestamps, load_1m_vals, label="Load Avg (1m)", color="#f43f5e", linewidth=1.2, linestyle=':')
        line_load_5m, = ax2.plot(timestamps, load_5m_vals, label="Load Avg (5m)", color="#ec4899", linewidth=1.2, linestyle='--')
        line_load_15m, = ax2.plot(timestamps, load_15m_vals, label="Load Avg (15m)", color="#a855f7", linewidth=1.2, linestyle='-.')
        
        ax2.set_ylabel("Load Average", color="#94a3b8", fontsize=9)
        max_load = max(
            max(load_1m_vals) if load_1m_vals else 1.0,
            max(load_5m_vals) if load_5m_vals else 1.0,
            max(load_15m_vals) if load_15m_vals else 1.0
        )
        ax2.set_ylim(-0.1, max(max_load * 1.2, 1.0))
        ax2.tick_params(axis='y', labelcolor="#94a3b8", labelsize=8)
        
        # Format x-axis time based on requested duration
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        if hours <= 3:
            ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
        elif hours <= 12:
            ax1.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        else:
            ax1.xaxis.set_major_locator(mdates.HourLocator(interval=4))
            
        ax1.tick_params(axis='x', labelcolor="#94a3b8", labelsize=8)
        fig.autofmt_xdate()
        
        # Combined Legend
        lines = [line_cpu, line_ram, line_load_1m, line_load_5m, line_load_15m]
        labels = [l.get_label() for l in lines]
        ax1.legend(lines, labels, loc="upper left", fontsize=8, facecolor='#1e293b', edgecolor='#334155')
        
        plt.tight_layout()
        plt.savefig(image_path, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close()
    except Exception as pe:
        print(f"[System Graph Plotting Error] {pe}")
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, f"Error rendering graph: {pe}", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()

def generate_network_graph(image_path: str, hours: int = 3):
    import sqlite3
    import time
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime
    
    # Query last N hours
    now = time.time()
    cutoff_time = now - hours * 3600
    
    timestamps = []
    net_up_vals = []
    net_down_vals = []
    
    try:
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT timestamp, net_sent_rate, net_recv_rate 
                FROM metrics 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
            
            for row in cursor.fetchall():
                ts = row["timestamp"]
                timestamps.append(datetime.fromtimestamp(ts))
                
                # Convert bytes/sec rate to Mbps
                net_up = (row["net_sent_rate"] * 8) / 10**6
                net_down = (row["net_recv_rate"] * 8) / 10**6
                net_up_vals.append(net_up)
                net_down_vals.append(net_down)
    except Exception as dbe:
        print(f"[Network Graph DB Error] {dbe}")
            
    if not timestamps:
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No metrics collected yet", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()
        return

    try:
        plt.style.use('dark_background')
        fig, ax1 = plt.subplots(figsize=(10, 5.2), dpi=200)
        fig.patch.set_facecolor('#0f172a') # Slate 900
        ax1.set_facecolor('#1e293b') # Slate 800
        
        plt.title(f"Network Traffic - Last {hours} Hours", fontsize=12, fontweight='bold', pad=12, color='#f8fafc')
        
        # Grid
        ax1.grid(True, linestyle='--', color='#334155', alpha=0.5)
        
        # Plot Network Rates (Mbps) on Y-axis
        line_down, = ax1.plot(timestamps, net_down_vals, label="Download (Mbps)", color="#8b5cf6", linewidth=1.5)
        line_up, = ax1.plot(timestamps, net_up_vals, label="Upload (Mbps)", color="#f59e0b", linewidth=1.5)
        
        ax1.set_ylabel("Speed (Mbps)", color="#94a3b8", fontsize=9)
        max_rate = max(
            max(net_up_vals) if net_up_vals else 1.0,
            max(net_down_vals) if net_down_vals else 1.0
        )
        ax1.set_ylim(-0.1, max(max_rate * 1.2, 1.0))
        ax1.tick_params(axis='y', labelcolor="#94a3b8", labelsize=8)
        
        # Format x-axis time based on requested duration
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        if hours <= 3:
            ax1.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
        elif hours <= 12:
            ax1.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        else:
            ax1.xaxis.set_major_locator(mdates.HourLocator(interval=4))
            
        ax1.tick_params(axis='x', labelcolor="#94a3b8", labelsize=8)
        fig.autofmt_xdate()
        
        ax1.legend(handles=[line_down, line_up], loc="upper left", fontsize=8, facecolor='#1e293b', edgecolor='#334155')
        
        plt.tight_layout()
        plt.savefig(image_path, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close()
    except Exception as pe:
        print(f"[Network Graph Plotting Error] {pe}")
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, f"Error rendering graph: {pe}", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()

def generate_cpu_graph(image_path: str, hours: int = 3):
    import sqlite3
    import time
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime

    now = time.time()
    cutoff_time = now - hours * 3600

    timestamps = []
    cpu_vals = []

    try:
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT timestamp, cpu_percent 
                FROM metrics 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
            for row in cursor.fetchall():
                ts = row["timestamp"]
                timestamps.append(datetime.fromtimestamp(ts))
                cpu_vals.append(row["cpu_percent"])
    except Exception as dbe:
        print(f"[CPU Graph DB Error] {dbe}")

    if not timestamps:
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No metrics collected yet", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()
        return

    try:
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5.2), dpi=200)
        fig.patch.set_facecolor('#0f172a') # Slate 900
        ax.set_facecolor('#1e293b') # Slate 800
        
        plt.title(f"CPU Usage - Last {hours} Hours", fontsize=12, fontweight='bold', pad=12, color='#f8fafc')
        ax.grid(True, linestyle='--', color='#334155', alpha=0.5)
        
        ax.plot(timestamps, cpu_vals, label="CPU (%)", color="#3b82f6", linewidth=1.5)
        ax.fill_between(timestamps, cpu_vals, color="#3b82f6", alpha=0.15)
        
        ax.set_ylabel("Usage (%)", color="#94a3b8", fontsize=9)
        ax.set_ylim(-2, 102)
        ax.tick_params(axis='y', labelcolor="#94a3b8", labelsize=8)
        
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        if hours <= 3:
            ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
        elif hours <= 12:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        else:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
            
        ax.tick_params(axis='x', labelcolor="#94a3b8", labelsize=8)
        fig.autofmt_xdate()
        ax.legend(loc="upper left", fontsize=8, facecolor='#1e293b', edgecolor='#334155')
        
        plt.tight_layout()
        plt.savefig(image_path, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close()
    except Exception as pe:
        print(f"[CPU Graph Plotting Error] {pe}")
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, f"Error rendering graph: {pe}", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()

def generate_ram_graph(image_path: str, hours: int = 3):
    import sqlite3
    import time
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime

    now = time.time()
    cutoff_time = now - hours * 3600

    timestamps = []
    ram_vals = []

    try:
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT timestamp, ram_percent 
                FROM metrics 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
            for row in cursor.fetchall():
                ts = row["timestamp"]
                timestamps.append(datetime.fromtimestamp(ts))
                ram_vals.append(row["ram_percent"])
    except Exception as dbe:
        print(f"[RAM Graph DB Error] {dbe}")

    if not timestamps:
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No metrics collected yet", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()
        return

    try:
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5.2), dpi=200)
        fig.patch.set_facecolor('#0f172a') # Slate 900
        ax.set_facecolor('#1e293b') # Slate 800
        
        plt.title(f"Memory Usage - Last {hours} Hours", fontsize=12, fontweight='bold', pad=12, color='#f8fafc')
        ax.grid(True, linestyle='--', color='#334155', alpha=0.5)
        
        ax.plot(timestamps, ram_vals, label="RAM (%)", color="#10b981", linewidth=1.5)
        ax.fill_between(timestamps, ram_vals, color="#10b981", alpha=0.15)
        
        ax.set_ylabel("Usage (%)", color="#94a3b8", fontsize=9)
        ax.set_ylim(-2, 102)
        ax.tick_params(axis='y', labelcolor="#94a3b8", labelsize=8)
        
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        if hours <= 3:
            ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
        elif hours <= 12:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        else:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
            
        ax.tick_params(axis='x', labelcolor="#94a3b8", labelsize=8)
        fig.autofmt_xdate()
        ax.legend(loc="upper left", fontsize=8, facecolor='#1e293b', edgecolor='#334155')
        
        plt.tight_layout()
        plt.savefig(image_path, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close()
    except Exception as pe:
        print(f"[RAM Graph Plotting Error] {pe}")
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, f"Error rendering graph: {pe}", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()

def generate_load_graph(image_path: str, hours: int = 3, plot_1m: bool = True, plot_5m: bool = True, plot_15m: bool = True):
    import sqlite3
    import time
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime
    import json

    now = time.time()
    cutoff_time = now - hours * 3600

    timestamps = []
    load_1m_vals = []
    load_5m_vals = []
    load_15m_vals = []

    try:
        with get_db() as conn:
            cursor = conn.execute("""
                SELECT timestamp, extra_json 
                FROM metrics 
                WHERE timestamp >= ? 
                ORDER BY timestamp ASC
            """, (cutoff_time,))
            for row in cursor.fetchall():
                ts = row["timestamp"]
                timestamps.append(datetime.fromtimestamp(ts))
                
                load_1m, load_5m, load_15m = 0.0, 0.0, 0.0
                try:
                    extra = json.loads(row["extra_json"])
                    if "load_avg" in extra and isinstance(extra["load_avg"], list):
                        l_avg = extra["load_avg"]
                        if len(l_avg) > 0: load_1m = l_avg[0]
                        if len(l_avg) > 1: load_5m = l_avg[1]
                        if len(l_avg) > 2: load_15m = l_avg[2]
                except Exception:
                    pass
                load_1m_vals.append(load_1m)
                load_5m_vals.append(load_5m)
                load_15m_vals.append(load_15m)
    except Exception as dbe:
        print(f"[Load Graph DB Error] {dbe}")

    if not timestamps:
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No metrics collected yet", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()
        return

    try:
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5.2), dpi=200)
        fig.patch.set_facecolor('#0f172a') # Slate 900
        ax.set_facecolor('#1e293b') # Slate 800
        
        plt.title(f"Load Average - Last {hours} Hours", fontsize=12, fontweight='bold', pad=12, color='#f8fafc')
        ax.grid(True, linestyle='--', color='#334155', alpha=0.5)
        
        lines = []
        if plot_1m:
            l1, = ax.plot(timestamps, load_1m_vals, label="1m", color="#ef4444", linewidth=1.5)
            lines.append(l1)
        if plot_5m:
            l5, = ax.plot(timestamps, load_5m_vals, label="5m", color="#f59e0b", linewidth=1.5, linestyle='--')
            lines.append(l5)
        if plot_15m:
            l15, = ax.plot(timestamps, load_15m_vals, label="15m", color="#8b5cf6", linewidth=1.5, linestyle='-.')
            lines.append(l15)
        
        ax.set_ylabel("Load Average", color="#94a3b8", fontsize=9)
        max_load = max(
            max(load_1m_vals) if load_1m_vals else 1.0,
            max(load_5m_vals) if load_5m_vals else 1.0,
            max(load_15m_vals) if load_15m_vals else 1.0
        )
        ax.set_ylim(-0.1, max(max_load * 1.2, 1.0))
        ax.tick_params(axis='y', labelcolor="#94a3b8", labelsize=8)
        
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        if hours <= 3:
            ax.xaxis.set_major_locator(mdates.MinuteLocator(interval=30))
        elif hours <= 12:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
        else:
            ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
            
        ax.tick_params(axis='x', labelcolor="#94a3b8", labelsize=8)
        fig.autofmt_xdate()
        if lines:
            ax.legend(handles=lines, loc="upper left", fontsize=8, facecolor='#1e293b', edgecolor='#334155')
        
        plt.tight_layout()
        plt.savefig(image_path, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close()
    except Exception as pe:
        print(f"[Load Graph Plotting Error] {pe}")
        plt.style.use('default')
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, f"Error rendering graph: {pe}", horizontalalignment='center', verticalalignment='center')
        plt.savefig(image_path, dpi=100)
        plt.close()

def generate_custom_graph(image_path: str, hours: int = 3):
    generate_system_graph(image_path, hours)

def generate_3h_graph(image_path: str):
    generate_system_graph(image_path, hours=3)

def send_telegram_photo(bot_token: str, chat_id: str, photo_path: str, caption: str = ""):
    import urllib.request
    import urllib.error
    import uuid
    import json
    
    boundary = f"----Boundary-{uuid.uuid4().hex}"
    
    with open(photo_path, "rb") as f:
        photo_data = f.read()
        
    body = []
    
    # chat_id
    body.append(f"--{boundary}".encode("utf-8"))
    body.append('Content-Disposition: form-data; name="chat_id"'.encode("utf-8"))
    body.append(''.encode("utf-8"))
    body.append(chat_id.encode("utf-8"))
    
    # caption
    if caption:
        body.append(f"--{boundary}".encode("utf-8"))
        body.append('Content-Disposition: form-data; name="caption"'.encode("utf-8"))
        body.append(''.encode("utf-8"))
        body.append(caption.encode("utf-8"))
        
        body.append(f"--{boundary}".encode("utf-8"))
        body.append('Content-Disposition: form-data; name="parse_mode"'.encode("utf-8"))
        body.append(''.encode("utf-8"))
        body.append("HTML".encode("utf-8"))
        
    # reply_markup
    body.append(f"--{boundary}".encode("utf-8"))
    body.append('Content-Disposition: form-data; name="reply_markup"'.encode("utf-8"))
    body.append(''.encode("utf-8"))
    markup = json.dumps({
        "keyboard": [
            [{"text": "📊 Check Status"}]
        ],
        "resize_keyboard": True
    })
    body.append(markup.encode("utf-8"))
        
    # photo file
    body.append(f"--{boundary}".encode("utf-8"))
    body.append('Content-Disposition: form-data; name="photo"; filename="graph.png"'.encode("utf-8"))
    body.append('Content-Type: image/png'.encode("utf-8"))
    body.append(''.encode("utf-8"))
    body.append(photo_data)
    
    body.append(f"--{boundary}--".encode("utf-8"))
    body_data = b"\r\n".join(body)
    
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        req = urllib.request.Request(url, data=body_data, method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        res_body = e.read().decode("utf-8", errors="ignore")
        print(f"[Telegram Photo Error] HTTP Error {e.code}: {e.reason} - Body: {res_body}")
        raise e
    except Exception as e:
        print(f"[Telegram Photo Error] {e}")
        raise e

def build_telegram_stats_message(now, cpu, ram_percent, disk_percent, load_avg=None):
    import socket
    import time
    import psutil
    import html
    if load_avg is None:
        try:
            load_avg = os.getloadavg()
        except (AttributeError, OSError):
            load_avg = (0.0, 0.0, 0.0)

    # Hostname
    hostname = html.escape(socket.gethostname())
    
    # Uptime
    boot_time = psutil.boot_time()
    uptime_sec = int(time.time() - boot_time)
    days = uptime_sec // 86400
    hours = (uptime_sec % 86400) // 3600
    minutes = (uptime_sec % 3600) // 60
    uptime_str = f"{days}d {hours}h {minutes}m"

    # CPU/RAM/Swap
    ram = psutil.virtual_memory()
    ram_used = round(ram.used / (1024 ** 3), 2)
    ram_total = round(ram.total / (1024 ** 3), 2)
    
    swap = psutil.swap_memory()
    swap_used = round(swap.used / (1024 ** 3), 2)
    swap_total = round(swap.total / (1024 ** 3), 2)

    # Disk
    disk = psutil.disk_usage("/")
    disk_used = round(disk.used / (1024 ** 3), 2)
    disk_total = round(disk.total / (1024 ** 3), 2)
    disk_speed = disk_tracker.get_speed()
    
    # Network
    net_speed = net_tracker.get_speed().get(DEFAULT_NIC, {
        "sent_bps": 0, "recv_bps": 0, "sent_total": 0, "recv_total": 0
    })
    upload_mbps = round(net_speed["sent_bps"] / 10**6, 2)
    download_mbps = round(net_speed["recv_bps"] / 10**6, 2)

    # Processes
    top_procs = _cached_top_procs[:8]
    proc_lines = []
    for p in top_procs:
        p_name = p["name"]
        if len(p_name) > 15:
            p_name = p_name[:12] + "..."
        line = f"{p['pid']:<6} {p_name:<15} {p['user']:<8} {p['cpu']:>4}% {p['mem']:>5}"
        proc_lines.append(html.escape(line))
    proc_text = "\n".join(proc_lines)

    msg = (
        f"📊 <b>[Server Monitor - {hostname}]</b>\n"
        f"🕒 Time: <code>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}</code>\n"
        f"⏱️ Uptime: <code>{uptime_str}</code>\n\n"
        
        f"🔹 <b>System Status:</b>\n"
        f"├─ CPU Usage: <b>{cpu}%</b>\n"
        f"├─ Load Avg: <b>{load_avg[0]:.2f} / {load_avg[1]:.2f} / {load_avg[2]:.2f}</b>\n"
        f"├─ Memory: <b>{ram_percent}%</b> ({ram_used}/{ram_total} GB)\n"
        f"└─ Swap: <b>{round(swap.percent, 1)}%</b> ({swap_used}/{swap_total} GB)\n\n"
        
        f"🔹 <b>Storage & Network:</b>\n"
        f"├─ Disk Usage: <b>{disk_percent}%</b> ({disk_used}/{disk_total} GB)\n"
        f"├─ Disk Read/Write: <b>{disk_speed['read_bps'] / 10**6:.2f} / {disk_speed['write_bps'] / 10**6:.2f} MB/s</b>\n"
        f"└─ Network Speed: <b>↑ {upload_mbps} / ↓ {download_mbps} Mbps</b>\n\n"
        
        f"🔥 <b>Top 8 Processes:</b>\n"
        f"<pre>PID    COMMAND         USER     CPU  MEM\n"
        f"{proc_text}</pre>"
    )
    return msg


class MetricsCollector:
    """Background collector that samples system metrics every COLLECT_INTERVAL seconds."""

    def __init__(self):
        self._prev_net = None  # Will be set after _get_default_nic_counters is defined
        self._prev_time = time.time()
        self._running = False
        self._thread = None
        self.last_alerts = {"cpu": 0.0, "ram": 0.0, "load": 0.0, "disk": 0.0}
        self._tg_bot_token = None
        self._tg_chat_id = None
        self._tg_interval_hours = 0
        self._tg_last_routine_sent = 0.0
        self._tg_send_graph = 0
        self._tg_enabled = 1
        self._tg_last_config_query_time = 0.0
        self._tg_last_routine_sent_local = 0.0
        self._tg_graph_hours = 3
        self._tg_custom_interval_minutes = 0
        self._tg_load_avg_type = 1
        self._tg_alert_send_graph = 0
        self._tg_send_sys_graph = 1
        self._tg_send_net_graph = 1
        self._tg_send_cpu_graph = 1
        self._tg_send_ram_graph = 1
        self._tg_send_load_graph = 1
        self._tg_send_load_1m_graph = 1
        self._tg_send_load_5m_graph = 1
        self._tg_send_load_15m_graph = 1

    def start(self):
        self._prev_net = _get_default_nic_counters()
        self._prev_time = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        
        self._tg_thread = threading.Thread(target=self._run_telegram_polling, daemon=True)
        self._tg_thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        last_collect = time.time()
        while self._running:
            try:
                now = time.time()
                if now - last_collect >= COLLECT_INTERVAL:
                    self._collect()
                    last_collect = now
                
                self._check_telegram_routine(now)
            except Exception as e:
                print(f"[Collector Loop Error] {e}")
            time.sleep(1)

    def _run_telegram_polling(self):
        import time
        import json
        import urllib.request
        import urllib.parse
        import urllib.error
        
        offset = 0
        while self._running:
            try:
                with get_db() as conn:
                    row = conn.execute("""
                        SELECT bot_token, chat_id, send_graph, enabled, graph_hours, send_sys_graph, send_net_graph, send_cpu_graph, send_ram_graph, send_load_1m_graph, send_load_5m_graph, send_load_15m_graph 
                        FROM telegram_config 
                        WHERE id = 1
                    """).fetchone()
                    
                if not row or not row["bot_token"] or not row["chat_id"] or row["enabled"] == 0:
                    time.sleep(5)
                    continue
                    
                bot_token = row["bot_token"]
                chat_id = row["chat_id"]
                send_graph = row["send_graph"]
                graph_hours = row["graph_hours"] if row["graph_hours"] is not None else 3
                send_sys_graph = row["send_sys_graph"] if row["send_sys_graph"] is not None else 1
                send_net_graph = row["send_net_graph"] if row["send_net_graph"] is not None else 1
                send_cpu_graph = row["send_cpu_graph"] if row["send_cpu_graph"] is not None else 1
                send_ram_graph = row["send_ram_graph"] if row["send_ram_graph"] is not None else 1
                send_load_1m_graph = row["send_load_1m_graph"] if row["send_load_1m_graph"] is not None else 1
                send_load_5m_graph = row["send_load_5m_graph"] if row["send_load_5m_graph"] is not None else 1
                send_load_15m_graph = row["send_load_15m_graph"] if row["send_load_15m_graph"] is not None else 1
                
                url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={offset}&timeout=10"
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=15) as response:
                    res_body = response.read().decode("utf-8")
                    data = json.loads(res_body)
                    
                    if not data.get("ok"):
                        time.sleep(5)
                        continue
                        
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        message = update.get("message")
                        if not message:
                            continue
                            
                        msg_chat_id = str(message["chat"]["id"])
                        if msg_chat_id != chat_id:
                            continue
                            
                        text = message.get("text", "").strip()
                        if text in ["/status", "📊 Check Status"]:
                            now = time.time()
                            cpu = psutil.cpu_percent()
                            ram_percent = psutil.virtual_memory().percent
                            disk_percent = psutil.disk_usage("/").percent
                            try:
                                load_avg = os.getloadavg()
                            except (AttributeError, OSError):
                                load_avg = (0.0, 0.0, 0.0)
                                
                            msg = build_telegram_stats_message(now, cpu, ram_percent, disk_percent, load_avg)
                            
                            graphs_to_send = []
                            if send_cpu_graph == 1:
                                graphs_to_send.append(("cpu", generate_cpu_graph, f"📊 <b>[CPU Usage - Last {graph_hours}h]</b>"))
                            if send_ram_graph == 1:
                                graphs_to_send.append(("ram", generate_ram_graph, f"📊 <b>[Memory Usage - Last {graph_hours}h]</b>"))
                            if (send_load_1m_graph == 1 or send_load_5m_graph == 1 or send_load_15m_graph == 1):
                                graphs_to_send.append((
                                    "load", 
                                    lambda p, hours: generate_load_graph(p, hours, plot_1m=(send_load_1m_graph==1), plot_5m=(send_load_5m_graph==1), plot_15m=(send_load_15m_graph==1)), 
                                    f"📊 <b>[Load Average - Last {graph_hours}h]</b>"
                                ))
                            if send_net_graph == 1:
                                graphs_to_send.append(("net", generate_network_graph, f"📊 <b>[Network Traffic - Last {graph_hours}h]</b>"))

                            if send_graph == 1 and graphs_to_send:
                                import tempfile
                                for i, (g_type, gen_fn, default_cap) in enumerate(graphs_to_send):
                                    caption = msg if i == 0 else default_cap
                                    tmp_path = None
                                    try:
                                        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                                            tmp_path = tmp_file.name
                                        gen_fn(tmp_path, graph_hours)
                                        send_telegram_photo(bot_token, chat_id, tmp_path, caption)
                                    except Exception as ge:
                                        print(f"[Telegram Graph Send Error] {g_type}: {ge}")
                                    finally:
                                        if tmp_path:
                                            try:
                                                os.unlink(tmp_path)
                                            except Exception:
                                                pass
                            else:
                                send_telegram_message(bot_token, chat_id, msg)
            except urllib.error.HTTPError as he:
                he_body = he.read().decode("utf-8", errors="ignore")
                print(f"[Telegram Polling HTTPError] Code {he.code}: {he.reason} - Body: {he_body}")
                time.sleep(10)
            except Exception as e:
                print(f"[Telegram Polling Exception] {e}")
                time.sleep(5)
            time.sleep(1)

    def _collect(self):
        now = time.time()
        elapsed = now - self._prev_time

        # CPU
        cpu = psutil.cpu_percent(interval=1)

        # RAM
        ram = psutil.virtual_memory()
        ram_percent = ram.percent
        ram_used_gb = round(ram.used / (1024 ** 3), 2)
        ram_total_gb = round(ram.total / (1024 ** 3), 2)

        # Disk
        disk = psutil.disk_usage("/")
        disk_percent = disk.percent
        disk_used_gb = round(disk.used / (1024 ** 3), 2)
        disk_total_gb = round(disk.total / (1024 ** 3), 2)

        # Network (default NIC only)
        net = _get_default_nic_counters()
        sent_rate = round((net.bytes_sent - self._prev_net.bytes_sent) / elapsed) if elapsed > 0 else 0
        recv_rate = round((net.bytes_recv - self._prev_net.bytes_recv) / elapsed) if elapsed > 0 else 0

        # Cap spikes at 10 Gbps (1.25 GB/s) to prevent invalid counter jumps from stretching Y-axis
        MAX_RATE = 1_250_000_000  # 10 Gbps in bytes/sec
        if sent_rate > MAX_RATE:
            sent_rate = MAX_RATE
        elif sent_rate < 0:
            sent_rate = 0

        if recv_rate > MAX_RATE:
            recv_rate = MAX_RATE
        elif recv_rate < 0:
            recv_rate = 0

        self._prev_net = net
        self._prev_time = now

        # Connections per interface
        conn_data = _get_connection_counts()
        conn_json_str = json.dumps(conn_data)
        
        # Load average (Linux only)
        try:
            load_avg = os.getloadavg()
        except (AttributeError, OSError):
            load_avg = (0.0, 0.0, 0.0)

        # Extra System Resources
        swap = psutil.swap_memory()
        cpu_cores = psutil.cpu_percent(percpu=True)
        disk_speed_data = disk_tracker.get_speed()
        extra_data = {
            "cpu_cores": cpu_cores,
            "ram_free_gb": round(ram.free / (1024 ** 3), 2),
            "ram_shared_gb": round(getattr(ram, 'shared', 0) / (1024 ** 3), 2),
            "ram_buff_cache_gb": round((getattr(ram, 'buffers', 0) + getattr(ram, 'cached', 0)) / (1024 ** 3), 2),
            "ram_available_gb": round(ram.available / (1024 ** 3), 2),
            "swap_used_gb": round(swap.used / (1024 ** 3), 2),
            "swap_total_gb": round(swap.total / (1024 ** 3), 2),
            "disk_read_bps": disk_speed_data.get("read_bps", 0),
            "disk_write_bps": disk_speed_data.get("write_bps", 0),
            "disk_read_iops": disk_speed_data.get("read_iops", 0),
            "disk_write_iops": disk_speed_data.get("write_iops", 0),
            "load_avg": [round(load_avg[0], 2), round(load_avg[1], 2), round(load_avg[2], 2)]
        }
        extra_json_str = json.dumps(extra_data)

        with get_db() as conn:
            conn.execute("""
                INSERT INTO metrics
                (timestamp, cpu_percent, ram_percent, ram_used_gb, ram_total_gb,
                 disk_percent, disk_used_gb, disk_total_gb,
                 net_sent_bytes, net_recv_bytes, net_sent_rate, net_recv_rate, conn_json, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now, cpu, ram_percent, ram_used_gb, ram_total_gb,
                disk_percent, disk_used_gb, disk_total_gb,
                net.bytes_sent, net.bytes_recv, sent_rate, recv_rate, conn_json_str, extra_json_str
            ))

        # Cleanup old data (run once every ~100 cycles ≈ every 50 minutes)
        if not hasattr(self, '_cleanup_counter'):
            self._cleanup_counter = 0
        self._cleanup_counter += 1
        if self._cleanup_counter >= 100:
            self._cleanup_counter = 0
            cutoff = now - (RETENTION_DAYS * 86400)
            with get_db() as conn:
                conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))

        # Check Telegram Alerts
        try:
            self._check_telegram_alerts(now, cpu, ram_percent, disk_percent)
        except Exception as te:
            print(f"[Telegram Alert Execution Error] {te}")

    def _check_telegram_alerts(self, now, cpu, ram_percent, disk_percent):
        import socket
        import time
        # Get load average
        try:
            load_avg = os.getloadavg()
        except (AttributeError, OSError):
            load_avg = (0.0, 0.0, 0.0)

        with get_db() as conn:
            row = conn.execute("""
                SELECT bot_token, chat_id, cpu_threshold, ram_threshold, load_threshold, disk_threshold, enabled, load_avg_type, alert_send_graph, graph_hours, send_sys_graph, send_net_graph, send_cpu_graph, send_ram_graph, send_load_graph, send_load_1m_graph, send_load_5m_graph, send_load_15m_graph
                FROM telegram_config
                WHERE id = 1
            """).fetchone()
            
            if not row or not row["bot_token"] or not row["chat_id"] or row["enabled"] == 0:
                return

            bot_token = row["bot_token"]
            chat_id = row["chat_id"]
            cpu_th = row["cpu_threshold"]
            ram_th = row["ram_threshold"]
            load_th = row["load_threshold"]
            disk_th = row["disk_threshold"]
            load_avg_type = row["load_avg_type"] if row["load_avg_type"] is not None else 1
            alert_send_graph = row["alert_send_graph"] if row["alert_send_graph"] is not None else 0
            graph_hours = row["graph_hours"] if row["graph_hours"] is not None else 3
            send_sys_graph = row["send_sys_graph"] if row["send_sys_graph"] is not None else 1
            send_net_graph = row["send_net_graph"] if row["send_net_graph"] is not None else 1
            send_cpu_graph = row["send_cpu_graph"] if row["send_cpu_graph"] is not None else 1
            send_ram_graph = row["send_ram_graph"] if row["send_ram_graph"] is not None else 1
            send_load_graph = row["send_load_graph"] if row["send_load_graph"] is not None else 1
            send_load_1m_graph = row["send_load_1m_graph"] if row["send_load_1m_graph"] is not None else 1
            send_load_5m_graph = row["send_load_5m_graph"] if row["send_load_5m_graph"] is not None else 1
            send_load_15m_graph = row["send_load_15m_graph"] if row["send_load_15m_graph"] is not None else 1

        # 1. Check Alert Thresholds
        alerts_triggered = []
        
        if cpu_th > 0 and cpu > cpu_th:
            if now - self.last_alerts.get("cpu", 0) > 900: # 15 minutes cooldown
                alerts_triggered.append(f"⚠️ CPU Usage: {cpu}% (Threshold: {cpu_th}%)")
                self.last_alerts["cpu"] = now

        if ram_th > 0 and ram_percent > ram_th:
            if now - self.last_alerts.get("ram", 0) > 900:
                alerts_triggered.append(f"⚠️ RAM Usage: {ram_percent}% (Threshold: {ram_th}%)")
                self.last_alerts["ram"] = now

        # Determine load average type index and label
        load_idx = 0
        load_label = "1m"
        if load_avg_type == 5:
            load_idx = 1
            load_label = "5m"
        elif load_avg_type == 15:
            load_idx = 2
            load_label = "15m"
            
        current_load = load_avg[load_idx]

        if load_th > 0 and current_load > load_th:
            if now - self.last_alerts.get("load", 0) > 900:
                alerts_triggered.append(f"⚠️ Load Average {load_label}: {current_load} (Threshold: {load_th})")
                self.last_alerts["load"] = now

        if disk_th > 0 and disk_percent > disk_th:
            if now - self.last_alerts.get("disk", 0) > 900:
                alerts_triggered.append(f"⚠️ Disk Usage: {disk_percent}% (Threshold: {disk_th}%)")
                self.last_alerts["disk"] = now

        # If any alerts triggered, send them
        if alerts_triggered:
            import html
            hostname = html.escape(socket.gethostname())
            alert_text = f"🚨 <b>[Alert - {hostname}]</b>\n\n" + "\n".join(alerts_triggered)
            alert_text += f"\n\n🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}"
            try:
                graphs_to_send = []
                if send_cpu_graph == 1:
                    graphs_to_send.append(("cpu", generate_cpu_graph, f"📊 <b>[CPU Usage - Last {graph_hours}h]</b>"))
                if send_ram_graph == 1:
                    graphs_to_send.append(("ram", generate_ram_graph, f"📊 <b>[Memory Usage - Last {graph_hours}h]</b>"))
                if (send_load_1m_graph == 1 or send_load_5m_graph == 1 or send_load_15m_graph == 1):
                    graphs_to_send.append((
                        "load", 
                        lambda p, hours: generate_load_graph(p, hours, plot_1m=(send_load_1m_graph==1), plot_5m=(send_load_5m_graph==1), plot_15m=(send_load_15m_graph==1)), 
                        f"📊 <b>[Load Average - Last {graph_hours}h]</b>"
                    ))
                if send_net_graph == 1:
                    graphs_to_send.append(("net", generate_network_graph, f"📊 <b>[Network Traffic - Last {graph_hours}h]</b>"))

                if alert_send_graph == 1 and graphs_to_send:
                    import tempfile
                    for i, (g_type, gen_fn, default_cap) in enumerate(graphs_to_send):
                        caption = alert_text if i == 0 else default_cap
                        tmp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                                tmp_path = tmp_file.name
                            gen_fn(tmp_path, hours=graph_hours)
                            send_telegram_photo(bot_token, chat_id, tmp_path, caption)
                        except Exception as ge:
                            print(f"[Telegram Alert Graph Send Error] {g_type}: {ge}")
                        finally:
                            if tmp_path:
                                try:
                                    os.unlink(tmp_path)
                                except Exception:
                                    pass
                else:
                    send_telegram_message(bot_token, chat_id, alert_text)
            except Exception as e:
                print(f"[Telegram Alert Error] {e}")

    def _check_telegram_routine(self, now):
        # Update config cache every 5 seconds
        if now - self._tg_last_config_query_time >= 5:
            try:
                with get_db() as conn:
                    row = conn.execute("""
                        SELECT bot_token, chat_id, interval_hours, last_routine_sent, send_graph, enabled, graph_hours, custom_interval_minutes, send_sys_graph, send_net_graph, send_cpu_graph, send_ram_graph, send_load_graph, send_load_1m_graph, send_load_5m_graph, send_load_15m_graph
                        FROM telegram_config
                        WHERE id = 1
                    """).fetchone()
                    if row:
                        self._tg_bot_token = row["bot_token"]
                        self._tg_chat_id = row["chat_id"]
                        self._tg_interval_hours = row["interval_hours"]
                        # If database value was reset to 0.0 (e.g. from UI), reset local memory tracker too
                        if row["last_routine_sent"] == 0.0 and self._tg_last_routine_sent != 0.0:
                            self._tg_last_routine_sent_local = 0.0
                        self._tg_last_routine_sent = row["last_routine_sent"]
                        self._tg_send_graph = row["send_graph"]
                        self._tg_enabled = row["enabled"]
                        self._tg_graph_hours = row["graph_hours"] if row["graph_hours"] is not None else 3
                        self._tg_custom_interval_minutes = row["custom_interval_minutes"] if row["custom_interval_minutes"] is not None else 0
                        self._tg_send_sys_graph = row["send_sys_graph"] if row["send_sys_graph"] is not None else 1
                        self._tg_send_net_graph = row["send_net_graph"] if row["send_net_graph"] is not None else 1
                        self._tg_send_cpu_graph = row["send_cpu_graph"] if row["send_cpu_graph"] is not None else 1
                        self._tg_send_ram_graph = row["send_ram_graph"] if row["send_ram_graph"] is not None else 1
                        self._tg_send_load_graph = row["send_load_graph"] if row["send_load_graph"] is not None else 1
                        self._tg_send_load_1m_graph = row["send_load_1m_graph"] if row["send_load_1m_graph"] is not None else 1
                        self._tg_send_load_5m_graph = row["send_load_5m_graph"] if row["send_load_5m_graph"] is not None else 1
                        self._tg_send_load_15m_graph = row["send_load_15m_graph"] if row["send_load_15m_graph"] is not None else 1
                self._tg_last_config_query_time = now
            except Exception as dbe:
                print(f"[Telegram Cache Query Error] {dbe}")

        bot_token = self._tg_bot_token
        chat_id = self._tg_chat_id
        interval_hours = self._tg_interval_hours
        last_routine_sent = self._tg_last_routine_sent
        send_graph = self._tg_send_graph
        enabled = self._tg_enabled
        graph_hours = self._tg_graph_hours
        custom_interval_minutes = self._tg_custom_interval_minutes
        send_sys_graph = self._tg_send_sys_graph
        send_net_graph = self._tg_send_net_graph
        send_cpu_graph = self._tg_send_cpu_graph
        send_ram_graph = self._tg_send_ram_graph
        send_load_graph = self._tg_send_load_graph
        send_load_1m_graph = self._tg_send_load_1m_graph
        send_load_5m_graph = self._tg_send_load_5m_graph
        send_load_15m_graph = self._tg_send_load_15m_graph

        if not bot_token or not chat_id or interval_hours == 0 or enabled == 0:
            return

        if interval_hours == -1:
            interval_seconds = custom_interval_minutes * 60
        else:
            interval_seconds = abs(interval_hours) * 60 if interval_hours < 0 else interval_hours * 3600

        if interval_seconds <= 0:
            return

        # Determine if we should trigger
        trigger = False
        aligned_time = 0.0
        aligned_time_now = float((int(now) // interval_seconds) * interval_seconds)
        
        if last_routine_sent == 0.0:
            if self._tg_last_routine_sent_local != aligned_time_now:
                trigger = True
                aligned_time = aligned_time_now
        elif now - last_routine_sent >= interval_seconds:
            if self._tg_last_routine_sent_local != aligned_time_now:
                trigger = True
                aligned_time = aligned_time_now
            
        if trigger:
            self._tg_last_routine_sent_local = aligned_time
            self._tg_last_routine_sent = aligned_time
            
            cpu = psutil.cpu_percent()
            ram_percent = psutil.virtual_memory().percent
            disk_percent = psutil.disk_usage("/").percent
            try:
                load_avg = os.getloadavg()
            except (AttributeError, OSError):
                load_avg = (0.0, 0.0, 0.0)
                
            msg = build_telegram_stats_message(now, cpu, ram_percent, disk_percent, load_avg)
            try:
                graphs_to_send = []
                if send_cpu_graph == 1:
                    graphs_to_send.append(("cpu", generate_cpu_graph, f"📊 <b>[CPU Usage - Last {graph_hours}h]</b>"))
                if send_ram_graph == 1:
                    graphs_to_send.append(("ram", generate_ram_graph, f"📊 <b>[Memory Usage - Last {graph_hours}h]</b>"))
                if (send_load_1m_graph == 1 or send_load_5m_graph == 1 or send_load_15m_graph == 1):
                    graphs_to_send.append((
                        "load",
                        lambda p, hours: generate_load_graph(p, hours, plot_1m=(send_load_1m_graph==1), plot_5m=(send_load_5m_graph==1), plot_15m=(send_load_15m_graph==1)),
                        f"📊 <b>[Load Average - Last {graph_hours}h]</b>"
                    ))
                if send_net_graph == 1:
                    graphs_to_send.append(("net", generate_network_graph, f"📊 <b>[Network Traffic - Last {graph_hours}h]</b>"))

                if send_graph == 1 and graphs_to_send:
                    import tempfile
                    for i, (g_type, gen_fn, default_cap) in enumerate(graphs_to_send):
                        caption = msg if i == 0 else default_cap
                        tmp_path = None
                        try:
                            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                                tmp_path = tmp_file.name
                            gen_fn(tmp_path, hours=graph_hours)
                            send_telegram_photo(bot_token, chat_id, tmp_path, caption)
                        except Exception as ge:
                            print(f"[Telegram Routine Graph Send Error] {g_type}: {ge}")
                        finally:
                            if tmp_path:
                                try:
                                    os.unlink(tmp_path)
                                except Exception:
                                    pass
                else:
                    send_telegram_message(bot_token, chat_id, msg)
                    
                try:
                    with get_db() as conn:
                        conn.execute("UPDATE telegram_config SET last_routine_sent = ? WHERE id = 1", (aligned_time,))
                except Exception as dbe:
                    print(f"[Telegram Routine DB Write Error] {dbe}")
            except Exception as e:
                # If sending failed, reset local guards so it will try again
                self._tg_last_routine_sent_local = 0.0
                self._tg_last_routine_sent = 0.0
                print(f"[Telegram Routine Error] {e}")


# ─── Network Interface Detection ─────────────────────────────────────────────

# Virtual / non-physical interface patterns to skip when picking defaults
_VIRTUAL_NIC_PATTERNS = re.compile(
    r'^(lo|docker\d*|veth|br-|virbr|tun|tap|wg|tailscale|dummy|bond_slave|sit|ip6tnl)'
)


def _detect_default_nic():
    """Auto-detect the primary physical NIC.

    Strategy: pick the first non-loopback, non-virtual interface that has
    an IPv4 address.  Falls back to the first non-loopback interface, or 'lo'.
    """
    stats = psutil.net_if_stats()   # {iface: snicstats}
    addrs = psutil.net_if_addrs()   # {iface: [snicaddr]}
    candidates = []

    for iface, st in stats.items():
        if _VIRTUAL_NIC_PATTERNS.match(iface):
            continue
        if not st.isup:
            continue
        # Prefer interfaces that have an IPv4 address
        has_ipv4 = any(
            a.family.name == 'AF_INET'
            for a in addrs.get(iface, [])
        )
        candidates.append((iface, has_ipv4))

    # Sort: prefer interfaces with IPv4
    candidates.sort(key=lambda x: (not x[1], x[0]))
    if candidates:
        return candidates[0][0]

    # Ultimate fallback
    for iface in stats:
        if iface != 'lo':
            return iface
    return 'lo'


DEFAULT_NIC = _detect_default_nic()


def _get_default_nic_counters():
    """Get network counters for the auto-detected default interface."""
    per_nic = psutil.net_io_counters(pernic=True)
    if DEFAULT_NIC in per_nic:
        return per_nic[DEFAULT_NIC]
    # Fallback to total if interface not found
    return psutil.net_io_counters()


def _get_all_nic_info():
    """Return list of all non-virtual network interfaces with metadata."""
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    result = []
    for iface in sorted(stats.keys()):
        if iface == 'lo':
            continue
        addr_list = []
        for a in addrs.get(iface, []):
            if a.family.name in ('AF_INET', 'AF_INET6'):
                addr_list.append({'family': a.family.name, 'address': a.address})
        result.append({
            'name': iface,
            'is_up': stats[iface].isup,
            'speed_mbps': stats[iface].speed,
            'is_default': iface == DEFAULT_NIC,
            'is_virtual': bool(_VIRTUAL_NIC_PATTERNS.match(iface)),
            'addrs': addr_list
        })
    return result


def _get_connection_counts():
    """Count TCP/UDP connections per network interface."""
    # Build IP -> interface map
    ip_to_iface = {}
    for iface, addrs in psutil.net_if_addrs().items():
        for addr in addrs:
            if addr.family in (socket.AF_INET, socket.AF_INET6):
                ip_to_iface[addr.address] = iface

    result = {}  # {iface: {"tcp": N, "udp": N}}
    total_tcp = 0
    total_udp = 0

    try:
        conns = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, OSError):
        return {"Total": {"tcp": 0, "udp": 0}}

    for c in conns:
        if not c.laddr:
            continue
        local_ip = c.laddr.ip
        iface = ip_to_iface.get(local_ip, "other")

        if iface not in result:
            result[iface] = {"tcp": 0, "udp": 0}

        if c.type == socket.SOCK_STREAM:  # TCP
            result[iface]["tcp"] += 1
            total_tcp += 1
        elif c.type == socket.SOCK_DGRAM:  # UDP
            result[iface]["udp"] += 1
            total_udp += 1

    result["Total"] = {"tcp": total_tcp, "udp": total_udp}
    return result


class NetworkSpeedTracker:
    """Tracks network speed and packet rate for ALL interfaces."""

    def __init__(self):
        self._prev_all = psutil.net_io_counters(pernic=True)
        self._prev_time = time.time()
        # Per-NIC calculated speeds
        self._speeds = {}  # {iface: {sent_bps, recv_bps, ...}}

    def get_speed(self, iface=None):
        """Return per-NIC speed dict. If iface given, return that NIC only."""
        now = time.time()
        cur_all = psutil.net_io_counters(pernic=True)
        elapsed = now - self._prev_time

        result = {}
        for nic, cur in cur_all.items():
            prev = self._prev_all.get(nic)
            if prev is None or elapsed <= 0:
                result[nic] = {
                    "sent_bps": 0, "recv_bps": 0,
                    "sent_pps": 0, "recv_pps": 0,
                    "sent_total": cur.bytes_sent,
                    "recv_total": cur.bytes_recv
                }
            else:
                result[nic] = {
                    "sent_bps": round(((cur.bytes_sent - prev.bytes_sent) / elapsed) * 8),
                    "recv_bps": round(((cur.bytes_recv - prev.bytes_recv) / elapsed) * 8),
                    "sent_pps": round((cur.packets_sent - prev.packets_sent) / elapsed),
                    "recv_pps": round((cur.packets_recv - prev.packets_recv) / elapsed),
                    "sent_total": cur.bytes_sent,
                    "recv_total": cur.bytes_recv
                }

        self._prev_all = cur_all
        self._prev_time = now
        self._speeds = result

        if iface and iface in result:
            return result[iface]
        return result


class DiskSpeedTracker:
    """Tracks disk I/O speed (bytes/sec and IOPS)."""

    def __init__(self):
        self._prev_disk = psutil.disk_io_counters()
        self._prev_time = time.time()
        self._read_bps = 0
        self._write_bps = 0
        self._read_iops = 0
        self._write_iops = 0

    def get_speed(self):
        now = time.time()
        disk = psutil.disk_io_counters()
        elapsed = now - self._prev_time

        if elapsed > 0 and disk and self._prev_disk:
            self._read_bps = (disk.read_bytes - self._prev_disk.read_bytes) / elapsed
            self._write_bps = (disk.write_bytes - self._prev_disk.write_bytes) / elapsed
            self._read_iops = (disk.read_count - self._prev_disk.read_count) / elapsed
            self._write_iops = (disk.write_count - self._prev_disk.write_count) / elapsed

        self._prev_disk = disk
        self._prev_time = now

        return {
            "read_bps": round(self._read_bps),
            "write_bps": round(self._write_bps),
            "read_iops": round(self._read_iops),
            "write_iops": round(self._write_iops)
        }


def parse_geosite_data(file_path):
    import os
    if not os.path.exists(file_path):
        return {}
    
    with open(file_path, "rb") as f:
        data = f.read()
        
    pos = 0
    categories = {}
    
    def read_varint(data, pos):
        result = 0
        shift = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    while pos < len(data):
        if pos >= len(data):
            break
        tag = data[pos]
        if tag != 10:
            pos += 1
            continue
        pos += 1
        
        entry_len, pos = read_varint(data, pos)
        entry_end = pos + entry_len
        
        country_code = ""
        domains = []
        
        while pos < entry_end:
            sub_tag, pos = read_varint(data, pos)
            wire_type = sub_tag & 0x07
            field_num = sub_tag >> 3
            
            if field_num == 1 and wire_type == 2:
                str_len, pos = read_varint(data, pos)
                country_code = data[pos:pos+str_len].decode('utf-8', 'ignore').upper()
                pos += str_len
            elif field_num == 2 and wire_type == 2:
                dom_len, pos = read_varint(data, pos)
                dom_end = pos + dom_len
                
                dom_type = 0
                dom_value = ""
                
                while pos < dom_end:
                    d_tag, pos = read_varint(data, pos)
                    d_wire = d_tag & 0x07
                    d_field = d_tag >> 3
                    
                    if d_field == 1 and d_wire == 0:
                        dom_type, pos = read_varint(data, pos)
                    elif d_field == 2 and d_wire == 2:
                        val_len, pos = read_varint(data, pos)
                        dom_value = data[pos:pos+val_len].decode('utf-8', 'ignore')
                        pos += val_len
                    else:
                        if d_wire == 0:
                            _, pos = read_varint(data, pos)
                        elif d_wire == 2:
                            skip_len, pos = read_varint(data, pos)
                            pos += skip_len
                        else:
                            pos += 1
                
                if dom_value:
                    domains.append((dom_type, dom_value))
                pos = dom_end
            else:
                if wire_type == 0:
                    _, pos = read_varint(data, pos)
                elif wire_type == 2:
                    skip_len, pos = read_varint(data, pos)
                    pos += skip_len
                else:
                    pos += 1
                    
        if country_code:
            categories[country_code] = domains
        pos = entry_end
        
    return categories


# ─── FastAPI App ─────────────────────────────────────────────────────────────

collector = MetricsCollector()
net_tracker = NetworkSpeedTracker()
disk_tracker = DiskSpeedTracker()
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

security = HTTPBasic()

def get_current_username(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = os.environ.get("PANEL_USERNAME", "admin").encode("utf8")
    correct_password = os.environ.get("PANEL_PASSWORD", "admin").encode("utf8")
    is_correct_username = credentials.username.encode("utf8") == correct_username
    is_correct_password = credentials.password.encode("utf8") == correct_password
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

_cpu_model_cache = None

def get_cpu_model_name():
    global _cpu_model_cache
    if _cpu_model_cache: return _cpu_model_cache
    try:
        proc_path = os.path.join(os.environ.get("PROCFS_PATH", "/proc"), "cpuinfo")
        with open(proc_path, "r") as f:
            for line in f:
                if line.startswith("model name"):
                    _cpu_model_cache = line.split(":", 1)[1].strip()
                    break
    except:
        pass
    if not _cpu_model_cache:
        import platform
        _cpu_model_cache = platform.processor() or "Unknown CPU"
    return _cpu_model_cache

@asynccontextmanager
async def lifespan(application):
    init_db()
    collector.start()
    yield
    collector.stop()


app = FastAPI(title="Server Monitor", lifespan=lifespan)

_benchmark_processes = []
_benchmark_lock = threading.Lock()
_benchmark_running = False

def _cpu_real_world_worker(duration_seconds: int, return_dict: dict, idx: int):
    """A CPU-bound task that emulates real-world server workloads: JSON parsing, SHA-256 cryptographic hashing, and sorting."""
    import os, time, json, hashlib, random
    try:
        os.nice(15)  # Lower priority so the system remains responsive
    except AttributeError:
        pass
        
    start = time.time()
    iterations = 0
    
    # Mock payload simulating real web API database structures
    payload = {
        "id": 10000 + (idx if isinstance(idx, int) else 99),
        "name": "Server Monitor Benchmark Client",
        "email": "benchmark@test.panel.rahanetmci.com",
        "roles": ["admin", "editor", "developer", "user"],
        "isActive": True,
        "system_stats": [random.random() for _ in range(120)]
    }
    
    while time.time() - start < duration_seconds:
        # 1. JSON Serialization & Deserialization
        serialized = json.dumps(payload)
        deserialized = json.loads(serialized)
        
        # 2. Cryptographic Hashing (simulating HTTPS/cookie security validation)
        data = (serialized * 12).encode("utf-8")  # ~12KB string
        h = hashlib.sha256(data).hexdigest()
        
        # 3. Memory Array Sorting (simulating database query ordering)
        arr = [random.randint(0, 100000) for _ in range(250)]
        arr.sort()
        
        iterations += 1
        
    return_dict[idx] = iterations

def _ram_speed_worker(duration_seconds: int, return_dict: dict):
    """A RAM-bound task to measure memory read-write allocation and copy throughput."""
    import os, time
    try:
        os.nice(15)
    except AttributeError:
        pass
        
    block_size = 10 * 1024 * 1024  # 10 MB
    data = bytearray(block_size)
    
    start = time.time()
    copied = 0
    while time.time() - start < duration_seconds:
        # Slice memory copy (fast C-level copy)
        data_copy = data[:]
        data_copy[0] = 1  # Force evaluation
        copied += block_size
        
    elapsed = time.time() - start
    gb_per_sec = (copied / (1024 ** 3)) / elapsed
    return_dict["ram_speed"] = gb_per_sec

def _disk_speed_worker(return_dict: dict):
    """A Disk-bound task that writes/reads a 50MB file with direct sync to measure IOPS and throughput."""
    import os, time
    try:
        os.nice(15)
    except AttributeError:
        pass
        
    file_path = "benchmark_temp.bin"
    block_size = 1024 * 1024  # 1 MB block
    block_data = os.urandom(block_size)
    blocks_count = 50  # 50 MB total file size
    
    # 1. Write Speed Test
    t0 = time.time()
    try:
        with open(file_path, "wb", buffering=0) as f:
            for _ in range(blocks_count):
                f.write(block_data)
                os.fsync(f.fileno())  # Bypass hypervisor write cache
        t1 = time.time()
        write_speed = blocks_count / (t1 - t0)  # MB/s
        
        # 2. Read Speed Test
        t0 = time.time()
        with open(file_path, "rb", buffering=0) as f:
            while f.read(block_size):
                pass
        t1 = time.time()
        read_speed = blocks_count / (t1 - t0)  # MB/s
        
        return_dict["disk_write"] = write_speed
        return_dict["disk_read"] = read_speed
    except Exception as e:
        return_dict["disk_error"] = str(e)
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

@app.get("/api/benchmark/cpu")
async def benchmark_cpu(username: str = Depends(get_current_username)):
    global _benchmark_processes, _benchmark_running
    cores = psutil.cpu_count(logical=True) or 1
    
    with _benchmark_lock:
        if _benchmark_running:
            return {"error": "Benchmark already running", "score": 0}
        _benchmark_running = True
        
    def run_benchmark():
        global _benchmark_processes
        ctx = multiprocessing.get_context("spawn")
        manager = ctx.Manager()
        results = manager.dict()
        
        try:
            # ─── Stage 1: CPU Single-Core (2 seconds) ───
            p_single = ctx.Process(target=_cpu_real_world_worker, args=(2, results, "cpu_single"))
            with _benchmark_lock:
                _benchmark_processes = [p_single]
            p_single.start()
            p_single.join()
            
            if p_single.exitcode != 0 and "cpu_single" not in results:
                return {"status": "stopped"}
                
            # ─── Stage 2: CPU Multi-Core (2 seconds) ───
            multi_dict = manager.dict()
            processes = []
            for i in range(cores):
                p = ctx.Process(target=_cpu_real_world_worker, args=(2, multi_dict, i))
                processes.append(p)
                
            with _benchmark_lock:
                _benchmark_processes = processes
                
            for p in processes:
                p.start()
            for p in processes:
                p.join()
                
            if any(p.exitcode != 0 for p in processes) and len(multi_dict) < cores:
                return {"status": "stopped"}
                
            # ─── Stage 3: RAM Bandwidth (1 second) ───
            p_ram = ctx.Process(target=_ram_speed_worker, args=(1, results))
            with _benchmark_lock:
                _benchmark_processes = [p_ram]
            p_ram.start()
            p_ram.join()
            
            if p_ram.exitcode != 0 and "ram_speed" not in results:
                return {"status": "stopped"}
                
            # ─── Stage 4: Disk I/O Speed (approx. 1.5 seconds) ───
            p_disk = ctx.Process(target=_disk_speed_worker, args=(results,))
            with _benchmark_lock:
                _benchmark_processes = [p_disk]
            p_disk.start()
            p_disk.join()
            
            if p_disk.exitcode != 0 and "disk_write" not in results:
                return {"status": "stopped"}
                
            # ─── Compile Results & Dimension Scores ───
            single_iters = results.get("cpu_single", 0)
            multi_iters = sum(multi_dict.values()) if multi_dict else 0
            ram_speed = results.get("ram_speed", 0.0)
            disk_write = results.get("disk_write", 0.0)
            disk_read = results.get("disk_read", 0.0)
            
            # Normalize sub-scores for standard VPS ranges
            # Average single-core core does ~1200 real-world tasks/sec
            cpu_single_score = int(single_iters * 0.9)
            # Multi-core scaling
            cpu_multi_score = int(multi_iters * 0.9)
            # Memory bandwidth: 15 GB/s = 1800 pts
            ram_score = int(ram_speed * 120)
            # Disk average speed: 500 MB/s = 750 pts
            disk_avg_speed = (disk_write + disk_read) / 2
            disk_score = int(disk_avg_speed * 1.5)
            
            # Overall Score (Weighted)
            overall_score = int(
                (cpu_single_score * 0.3) +
                (cpu_multi_score * 0.3) +
                (ram_score * 0.2) +
                (disk_score * 0.2)
            )
            
            return {
                "status": "success",
                "score": max(50, overall_score),
                "cpu_single_score": cpu_single_score,
                "cpu_single_val": single_iters,
                "cpu_multi_score": cpu_multi_score,
                "cpu_multi_val": multi_iters,
                "ram_score": ram_score,
                "ram_val_gbps": round(ram_speed, 2),
                "disk_score": disk_score,
                "disk_write_mbps": round(disk_write, 1),
                "disk_read_mbps": round(disk_read, 1),
                "cores": cores
            }
        except Exception as e:
            return {"error": f"Benchmark failed: {str(e)}", "score": 0}
        finally:
            # Cleanup temp file
            if os.path.exists("benchmark_temp.bin"):
                try:
                    os.remove("benchmark_temp.bin")
                except:
                    pass
                    
    loop = asyncio.get_running_loop()
    try:
        res_data = await loop.run_in_executor(None, run_benchmark)
    finally:
        with _benchmark_lock:
            _benchmark_processes = []
            _benchmark_running = False
            
    return res_data

@app.get("/api/benchmark/cpu/stop")
async def stop_benchmark_cpu(username: str = Depends(get_current_username)):
    global _benchmark_processes, _benchmark_running
    with _benchmark_lock:
        for p in _benchmark_processes:
            if p.is_alive():
                p.terminate()
        _benchmark_processes = []
        _benchmark_running = False
    return {"status": "stopped"}


# ─── Speedtest Endpoints ───────────────────────────────────────────

import speedtest

@app.get("/api/speedtest/servers")
async def get_speedtest_servers(username: str = Depends(get_current_username)):
    def run_get_servers():
        try:
            s = speedtest.Speedtest()
            servers = s.get_servers()
            flat_servers = []
            for d in sorted(servers.keys()):
                for srv in servers[d]:
                    flat_servers.append({
                        "id": srv["id"],
                        "sponsor": srv["sponsor"],
                        "name": srv["name"],
                        "country": srv["country"],
                        "d": round(srv["d"], 1)
                    })
            return flat_servers
        except Exception as e:
            return {"error": str(e)}
        
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, run_get_servers)
        if isinstance(res, dict) and "error" in res:
            return {"status": "error", "error": res["error"]}
        return {"status": "success", "servers": res}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/api/speedtest/run")
async def run_speedtest(server_id: str = None, username: str = Depends(get_current_username)):
    def run_test():
        s = speedtest.Speedtest()
        if server_id:
            try:
                s.get_servers([server_id])
            except Exception:
                pass # Fallback to auto
        else:
            s.get_servers()
        s.get_best_server()
        s.download()
        s.upload()
        return s.results.dict()

    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, run_test)
        return {"status": "success", "result": {
            "download": res["download"] / 10**6, # Mbps
            "upload": res["upload"] / 10**6, # Mbps
            "ping": res["ping"],
            "server": res["server"]
        }}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── Settings & Database Endpoints ───────────────────────────────────────────

class SSLSettings(BaseModel):
    certificate_pem: str
    private_key_pem: str

class TelegramConfigPayload(BaseModel):
    bot_token: str
    chat_id: str
    interval_hours: int
    cpu_threshold: float
    ram_threshold: float
    load_threshold: float
    disk_threshold: float
    send_graph: int = 0
    enabled: int = 1
    graph_hours: int = 3
    custom_interval_minutes: int = 0
    load_avg_type: int = 1
    alert_send_graph: int = 0
    send_sys_graph: int = 1
    send_net_graph: int = 1
    send_cpu_graph: int = 1
    send_ram_graph: int = 1
    send_load_graph: int = 1
    send_load_1m_graph: int = 1
    send_load_5m_graph: int = 1
    send_load_15m_graph: int = 1

class TelegramTestPayload(BaseModel):
    bot_token: str = None
    chat_id: str = None
    send_graph: int = None
    graph_hours: int = None
    send_sys_graph: int = None
    send_net_graph: int = None
    send_cpu_graph: int = None
    send_ram_graph: int = None
    send_load_graph: int = None
    send_load_1m_graph: int = None
    send_load_5m_graph: int = None
    send_load_15m_graph: int = None

@app.get("/api/telegram/config")
async def get_telegram_config(username: str = Depends(get_current_username)):
    with get_db() as conn:
        row = conn.execute("""
            SELECT bot_token, chat_id, interval_hours, cpu_threshold, ram_threshold, load_threshold, disk_threshold, send_graph, enabled, graph_hours, custom_interval_minutes, load_avg_type, alert_send_graph, send_sys_graph, send_net_graph, send_cpu_graph, send_ram_graph, send_load_graph, send_load_1m_graph, send_load_5m_graph, send_load_15m_graph
            FROM telegram_config
            WHERE id = 1
        """).fetchone()
        if not row:
            return JSONResponse(content={
                "bot_token": "",
                "chat_id": "",
                "interval_hours": 0,
                "cpu_threshold": 0.0,
                "ram_threshold": 0.0,
                "load_threshold": 0.0,
                "disk_threshold": 0.0,
                "send_graph": 0,
                "enabled": 1,
                "graph_hours": 3,
                "custom_interval_minutes": 0,
                "load_avg_type": 1,
                "alert_send_graph": 0,
                "send_sys_graph": 1,
                "send_net_graph": 1,
                "send_cpu_graph": 1,
                "send_ram_graph": 1,
                "send_load_graph": 1,
                "send_load_1m_graph": 1,
                "send_load_5m_graph": 1,
                "send_load_15m_graph": 1
            })
        return JSONResponse(content={
            "bot_token": row["bot_token"],
            "chat_id": row["chat_id"],
            "interval_hours": row["interval_hours"],
            "cpu_threshold": row["cpu_threshold"],
            "ram_threshold": row["ram_threshold"],
            "load_threshold": row["load_threshold"],
            "disk_threshold": row["disk_threshold"],
            "send_graph": row["send_graph"],
            "enabled": row["enabled"],
            "graph_hours": row["graph_hours"] if row["graph_hours"] is not None else 3,
            "custom_interval_minutes": row["custom_interval_minutes"] if row["custom_interval_minutes"] is not None else 0,
            "load_avg_type": row["load_avg_type"] if row["load_avg_type"] is not None else 1,
            "alert_send_graph": row["alert_send_graph"] if row["alert_send_graph"] is not None else 0,
            "send_sys_graph": row["send_sys_graph"] if row["send_sys_graph"] is not None else 1,
            "send_net_graph": row["send_net_graph"] if row["send_net_graph"] is not None else 1,
            "send_cpu_graph": row["send_cpu_graph"] if row["send_cpu_graph"] is not None else 1,
            "send_ram_graph": row["send_ram_graph"] if row["send_ram_graph"] is not None else 1,
            "send_load_graph": row["send_load_graph"] if row["send_load_graph"] is not None else 1,
            "send_load_1m_graph": row["send_load_1m_graph"] if row["send_load_1m_graph"] is not None else 1,
            "send_load_5m_graph": row["send_load_5m_graph"] if row["send_load_5m_graph"] is not None else 1,
            "send_load_15m_graph": row["send_load_15m_graph"] if row["send_load_15m_graph"] is not None else 1
        })

@app.post("/api/telegram/config")
async def save_telegram_config(payload: TelegramConfigPayload, username: str = Depends(get_current_username)):
    with get_db() as conn:
        conn.execute("""
            UPDATE telegram_config
            SET bot_token = ?,
                chat_id = ?,
                interval_hours = ?,
                cpu_threshold = ?,
                ram_threshold = ?,
                load_threshold = ?,
                disk_threshold = ?,
                send_graph = ?,
                enabled = ?,
                graph_hours = ?,
                custom_interval_minutes = ?,
                load_avg_type = ?,
                alert_send_graph = ?,
                send_sys_graph = ?,
                send_net_graph = ?,
                send_cpu_graph = ?,
                send_ram_graph = ?,
                send_load_graph = ?,
                send_load_1m_graph = ?,
                send_load_5m_graph = ?,
                send_load_15m_graph = ?,
                last_routine_sent = 0.0
            WHERE id = 1
        """, (
            payload.bot_token,
            payload.chat_id,
            payload.interval_hours,
            payload.cpu_threshold,
            payload.ram_threshold,
            payload.load_threshold,
            payload.disk_threshold,
            payload.send_graph,
            payload.enabled,
            payload.graph_hours,
            payload.custom_interval_minutes,
            payload.load_avg_type,
            payload.alert_send_graph,
            payload.send_sys_graph,
            payload.send_net_graph,
            payload.send_cpu_graph,
            payload.send_ram_graph,
            payload.send_load_graph,
            payload.send_load_1m_graph,
            payload.send_load_5m_graph,
            payload.send_load_15m_graph
        ))
    return JSONResponse(content={"status": "success", "message": "Configuration saved"})

@app.post("/api/telegram/test")
async def test_telegram_config(payload: TelegramTestPayload, username: str = Depends(get_current_username)):
    bot_token = payload.bot_token
    chat_id = payload.chat_id
    send_graph = payload.send_graph
    
    # Fallback to saved if not provided in payload
    with get_db() as conn:
        row = conn.execute("SELECT bot_token, chat_id, send_graph, graph_hours, send_sys_graph, send_net_graph, send_cpu_graph, send_ram_graph, send_load_graph, send_load_1m_graph, send_load_5m_graph, send_load_15m_graph FROM telegram_config WHERE id = 1").fetchone()
        if row:
            if not bot_token: bot_token = row["bot_token"]
            if not chat_id: chat_id = row["chat_id"]
            if send_graph is None: send_graph = row["send_graph"]
            graph_hours = payload.graph_hours if payload.graph_hours is not None else (row["graph_hours"] if row["graph_hours"] is not None else 3)
            send_cpu_graph = payload.send_cpu_graph if payload.send_cpu_graph is not None else (row["send_cpu_graph"] if row["send_cpu_graph"] is not None else 1)
            send_ram_graph = payload.send_ram_graph if payload.send_ram_graph is not None else (row["send_ram_graph"] if row["send_ram_graph"] is not None else 1)
            send_load_graph = payload.send_load_graph if payload.send_load_graph is not None else (row["send_load_graph"] if row["send_load_graph"] is not None else 1)
            send_net_graph = payload.send_net_graph if payload.send_net_graph is not None else (row["send_net_graph"] if row["send_net_graph"] is not None else 1)
            send_load_1m_graph = payload.send_load_1m_graph if payload.send_load_1m_graph is not None else (row["send_load_1m_graph"] if row["send_load_1m_graph"] is not None else 1)
            send_load_5m_graph = payload.send_load_5m_graph if payload.send_load_5m_graph is not None else (row["send_load_5m_graph"] if row["send_load_5m_graph"] is not None else 1)
            send_load_15m_graph = payload.send_load_15m_graph if payload.send_load_15m_graph is not None else (row["send_load_15m_graph"] if row["send_load_15m_graph"] is not None else 1)
        else:
            graph_hours = payload.graph_hours if payload.graph_hours is not None else 3
            send_cpu_graph = payload.send_cpu_graph if payload.send_cpu_graph is not None else 1
            send_ram_graph = payload.send_ram_graph if payload.send_ram_graph is not None else 1
            send_load_graph = payload.send_load_graph if payload.send_load_graph is not None else 1
            send_net_graph = payload.send_net_graph if payload.send_net_graph is not None else 1
            send_load_1m_graph = payload.send_load_1m_graph if payload.send_load_1m_graph is not None else 1
            send_load_5m_graph = payload.send_load_5m_graph if payload.send_load_5m_graph is not None else 1
            send_load_15m_graph = payload.send_load_15m_graph if payload.send_load_15m_graph is not None else 1
                
    if not bot_token or not chat_id:
        raise HTTPException(status_code=400, detail="Bot Token and Chat ID are required")
        
    try:
        now = time.time()
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        
        msg = "🧪 <b>[Server Monitor - Test Message]</b>\n"
        msg += "This is a test notification from your server.\n\n"
        msg += build_telegram_stats_message(now, cpu, ram.percent, disk.percent)
        
        graphs_to_send = []
        if send_cpu_graph == 1:
            graphs_to_send.append(("cpu", generate_cpu_graph, f"📊 <b>[CPU Usage - Last {graph_hours}h]</b>"))
        if send_ram_graph == 1:
            graphs_to_send.append(("ram", generate_ram_graph, f"📊 <b>[Memory Usage - Last {graph_hours}h]</b>"))
        if (send_load_1m_graph == 1 or send_load_5m_graph == 1 or send_load_15m_graph == 1):
            graphs_to_send.append((
                "load",
                lambda p, hours: generate_load_graph(p, hours, plot_1m=(send_load_1m_graph==1), plot_5m=(send_load_5m_graph==1), plot_15m=(send_load_15m_graph==1)),
                f"📊 <b>[Load Average - Last {graph_hours}h]</b>"
            ))
        if send_net_graph == 1:
            graphs_to_send.append(("net", generate_network_graph, f"📊 <b>[Network Traffic - Last {graph_hours}h]</b>"))

        if send_graph == 1 and graphs_to_send:
            import tempfile
            for i, (g_type, gen_fn, default_cap) in enumerate(graphs_to_send):
                caption = msg if i == 0 else default_cap
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
                        tmp_path = tmp_file.name
                    gen_fn(tmp_path, hours=graph_hours)
                    send_telegram_photo(bot_token, chat_id, tmp_path, caption)
                except Exception as ge:
                    print(f"[Telegram Test Graph Send Error] {g_type}: {ge}")
                finally:
                    if tmp_path:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
        else:
            send_telegram_message(bot_token, chat_id, msg)
            
        return JSONResponse(content={"status": "success", "message": "Test message sent successfully"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send message: {str(e)}")

@app.get("/api/backup")
async def download_db(username: str = Depends(get_current_username)):
    """Download the current metrics.db file."""
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=404, detail="Database file not found.")
    return FileResponse(DB_PATH, media_type="application/octet-stream", filename="metrics.db")

@app.post("/api/restore")
async def restore_db(file: UploadFile = File(...), username: str = Depends(get_current_username)):
    """Upload a metrics.db file and overwrite the current one."""
    if not file.filename.endswith((".db", ".sqlite")):
        raise HTTPException(status_code=400, detail="Invalid file type. Must be .db or .sqlite")
        
    content = await file.read()
    
    # Write to a temporary file first for safety
    temp_db = DB_PATH + ".tmp"
    with open(temp_db, "wb") as f:
        f.write(content)
        
    try:
        # Check if it's a valid SQLite DB
        test_conn = sqlite3.connect(temp_db)
        test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        test_conn.close()
    except Exception as e:
        os.remove(temp_db)
        raise HTTPException(status_code=400, detail=f"Corrupted or invalid database: {e}")
        
    # Replace live DB
    import shutil
    shutil.move(temp_db, DB_PATH)
    
    # Background restart
    def restart_server():
        time.sleep(2)
        os.system("systemctl restart server-monitor.service")
        
    threading.Thread(target=restart_server).start()
    return {"status": "Database restored successfully. Server restarting."}

@app.post("/api/settings")
async def update_settings(settings: SSLSettings, username: str = Depends(get_current_username)):
    """Apply SSL settings by modifying the systemd service and restarting."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    cert_path = os.path.join(app_dir, "cert.pem")
    key_path = os.path.join(app_dir, "key.pem")
    
    has_ssl = bool(settings.certificate_pem.strip() and settings.private_key_pem.strip())
    
    if has_ssl:
        with open(cert_path, "w") as f:
            f.write(settings.certificate_pem)
        with open(key_path, "w") as f:
            f.write(settings.private_key_pem)
    
    service_file = "/etc/systemd/system/server-monitor.service"
    if os.path.exists(service_file):
        with open(service_file, "r") as f:
            content = f.read()
            
        ssl_args = f"--ssl-keyfile={key_path} --ssl-certfile={cert_path}"
        
        # Clean existing SSL args if present
        content = re.sub(r' --ssl-keyfile=\S+ --ssl-certfile=\S+', '', content)
        
        if has_ssl:
            # Inject SSL args
            content = re.sub(r'(ExecStart=.*?uvicorn app:app .*?--port \d+)', r'\1 ' + ssl_args, content)
            
        with open(service_file, "w") as f:
            f.write(content)
            
        # Background daemon reload and restart
        def apply_changes():
            time.sleep(2)
            os.system("systemctl daemon-reload && systemctl restart server-monitor.service")
            
        threading.Thread(target=apply_changes).start()
        
    return {"status": "Settings applied. Service restarting."}


@app.post("/api/update")
async def update_panel(skip_git: bool = Query(False), username: str = Depends(get_current_username)):
    """Trigger systemd-run to execute install.sh to pull and upgrade the panel detached."""
    import subprocess

    # Detect Docker environment - containers cannot self-update via systemd
    is_docker = os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER") == "true"
    if is_docker:
        return {
            "error": "docker",
            "message": (
                "This panel is running inside a Docker container and cannot self-update.\n"
                "To update, run the following commands on your host server:\n\n"
                "  docker-compose pull\n"
                "  docker-compose up -d\n\n"
                "Or with docker run:\n"
                "  docker pull reza13721205/server-monitor:latest\n"
                "  docker stop server-monitor && docker rm server-monitor\n"
                "  docker run -d ... reza13721205/server-monitor:latest"
            )
        }

    install_script = "/opt/server-monitor/install.sh"
    if not os.path.exists(install_script):
        # Fallback if installed in a different folder
        install_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
        
    if not os.path.exists(install_script):
        return {"error": "install.sh script not found."}
        
    try:
        # Launch detached via systemd-run so it survives the panel service stop/restart
        unit_name = f"server-monitor-update-{int(time.time())}"
        
        env_vars = []
        if skip_git:
            env_vars = ["--setenv=SKIP_GIT=true"]
        
        # Build systemd-run command - use simple flags compatible with older systemd versions
        if skip_git:
            cmd = [
                "systemd-run", f"--unit={unit_name}", f"--description=Server Monitor Self Update"
            ] + env_vars + ["bash", install_script, "upgrade"]
        else:
            # Download and run the absolute latest installer to avoid bugs in older local scripts
            cmd_str = f"curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/install.sh | bash -s -- upgrade > /tmp/server-monitor-update.log 2>&1"
            cmd = [
                "systemd-run", f"--unit={unit_name}", f"--description=Server Monitor Self Update",
                "bash", "-c", cmd_str
            ]
        
        # Use subprocess.run with a short timeout just to launch - if systemd-run fails, fall through to nohup
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return {"status": "success", "message": "Update started in background. The panel will restart in a few seconds."}
        else:
            raise Exception(f"systemd-run failed: {result.stderr}")
    except Exception as e:
        # Fallback to setsid double-fork nohup if systemd-run is not available or fails
        try:
            if skip_git:
                cmd_str = f"nohup SKIP_GIT=true bash {install_script} upgrade > /tmp/server-monitor-update.log 2>&1 &"
            else:
                cmd_str = f"nohup bash -c 'curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/install.sh | bash -s -- upgrade > /tmp/server-monitor-update.log 2>&1' &"
            subprocess.Popen(cmd_str, shell=True, preexec_fn=os.setsid)
            return {"status": "success", "message": "Update started via fallback background process."}
        except Exception as err:
            return {"error": f"Failed to launch update process: {str(e)} (Fallback error: {str(err)})"}


@app.get("/")
async def index(username: str = Depends(get_current_username)):
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


@app.get("/api/interfaces")
async def list_interfaces(username: str = Depends(get_current_username)):
    """Return list of network interfaces with metadata."""
    return {"default": DEFAULT_NIC, "interfaces": _get_all_nic_info()}


_current_cache = {"data": None, "ts": 0}

@app.get("/api/current")
async def current_metrics(username: str = Depends(get_current_username)):
    """Return current system metrics snapshot (cached for 1 second)."""
    now = time.time()
    if _current_cache["data"] is not None and (now - _current_cache["ts"]) < 1.0:
        return _current_cache["data"]

    cpu = psutil.cpu_percent(interval=0)
    cpu_per_core = psutil.cpu_percent(percpu=True)
    ram = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage("/")
    all_speeds = net_tracker.get_speed()   # dict of all NICs
    disk_speed = disk_tracker.get_speed()

    # Default NIC speed for backward compat
    def_speed = all_speeds.get(DEFAULT_NIC, {
        "sent_bps": 0, "recv_bps": 0, "sent_pps": 0, "recv_pps": 0,
        "sent_total": 0, "recv_total": 0
    })

    # CPU info
    cpu_count = psutil.cpu_count()
    cpu_freq = psutil.cpu_freq()
    freq_current = round(cpu_freq.current, 0) if cpu_freq else 0

    # Uptime
    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    # Load average (Linux only)
    try:
        load_avg = os.getloadavg()
    except (AttributeError, OSError):
        load_avg = [0, 0, 0]

    # OS info
    try:
        if hasattr(platform, 'freedesktop_os_release'):
            os_release = platform.freedesktop_os_release()
            distro_name = os_release.get('PRETTY_NAME', platform.platform())
        else:
            distro_name = platform.platform()
    except OSError:
        distro_name = platform.platform()

    os_info = {
        "system": platform.system(),
        "release": platform.release(),
        "distro": distro_name
    }

    # Build per-NIC speed block (only non-lo)
    per_nic = {}
    for nic, spd in all_speeds.items():
        if nic == 'lo':
            continue
        per_nic[nic] = {
            "sent_bps": spd["sent_bps"],
            "recv_bps": spd["recv_bps"],
            "sent_pps": spd["sent_pps"],
            "recv_pps": spd["recv_pps"],
            "sent_gb": round(spd["sent_total"] / (1024 ** 3), 2),
            "recv_gb": round(spd["recv_total"] / (1024 ** 3), 2)
        }

    result = {
        "cpu": {
            "model": get_cpu_model_name(),
            "percent": cpu,
            "cores": cpu_count,
            "freq_mhz": freq_current,
            "per_core": cpu_per_core
        },
        "ram": {
            "percent": ram.percent,
            "used_gb": round(ram.used / (1024 ** 3), 2),
            "total_gb": round(ram.total / (1024 ** 3), 2),
            "available_gb": round(ram.available / (1024 ** 3), 2),
            "free_gb": round(ram.free / (1024 ** 3), 2),
            "shared_gb": round(getattr(ram, 'shared', 0) / (1024 ** 3), 2),
            "buff_cache_gb": round((getattr(ram, 'buffers', 0) + getattr(ram, 'cached', 0)) / (1024 ** 3), 2)
        },
        "swap": {
            "percent": swap.percent,
            "used_gb": round(swap.used / (1024 ** 3), 2),
            "total_gb": round(swap.total / (1024 ** 3), 2),
            "free_gb": round(swap.free / (1024 ** 3), 2)
        },
        "disk": {
            "percent": disk.percent,
            "used_gb": round(disk.used / (1024 ** 3), 2),
            "total_gb": round(disk.total / (1024 ** 3), 2),
            "free_gb": round(disk.free / (1024 ** 3), 2),
            "read_bps": disk_speed["read_bps"],
            "write_bps": disk_speed["write_bps"],
            "read_iops": disk_speed["read_iops"],
            "write_iops": disk_speed["write_iops"]
        },
        "network": {
            "default_nic": DEFAULT_NIC,
            "sent_bps": def_speed["sent_bps"],
            "recv_bps": def_speed["recv_bps"],
            "sent_pps": def_speed["sent_pps"],
            "recv_pps": def_speed["recv_pps"],
            "sent_gb": round(def_speed["sent_total"] / (1024 ** 3), 2),
            "recv_gb": round(def_speed["recv_total"] / (1024 ** 3), 2)
        },
        "per_nic": per_nic,
        "system": {
            "hostname": socket.gethostname(),
            "uptime_seconds": uptime_seconds,
            "load_avg_1m": round(load_avg[0], 2),
            "load_avg_5m": round(load_avg[1], 2),
            "load_avg_15m": round(load_avg[2], 2),
            "os": os_info,
            "version": VERSION
        },
        "connections": _get_connection_counts(),
        "public_ips": dict(zip(("ipv4", "ipv6"), get_public_ips())),
        "features": {
            "v2ray": ENABLE_V2RAY
        }
    }

    _current_cache["data"] = result
    _current_cache["ts"] = now
    return result


# global storage for V2ray speed tracking (safe: uses sqlite3 CLI, never opens DB directly)
v2ray_prev_bytes = {}  # {email: [{"up": int, "down": int, "time": float}]}
v2ray_lock = threading.Lock()
v2ray_cached_results = []
v2ray_last_update = 0.0
v2ray_ip_counts = {}  # {email: set_of_ips}

def get_xui_db_config():
    db_type = "sqlite"
    db_dsn = None
    env_file = "/etc/default/x-ui"
    if os.path.exists(env_file):
        try:
            with open(env_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        parts = line.split("=", 1)
                        key = parts[0].strip()
                        val = parts[1].strip().strip('"').strip("'")
                        if key == "XUI_DB_TYPE":
                            db_type = val
                        elif key == "XUI_DB_DSN":
                            db_dsn = val
        except Exception as e:
            print(f"Error reading {env_file}: {e}")
    return db_type, db_dsn

def query_postgres_direct(db_dsn):
    """Attempt to query PostgreSQL directly using pg8000 or psycopg2.
    Returns a list of tuples: (email, up, down, last_online, expiry_time, total, enable) or raises Exception."""
    # 1. Try importing pg8000 first (pure python)
    try:
        import pg8000
        params = {}
        if db_dsn.startswith("postgresql://") or db_dsn.startswith("postgres://"):
            from urllib.parse import urlparse
            url = urlparse(db_dsn)
            if url.username:
                params['user'] = url.username
            if url.password:
                params['password'] = url.password
            if url.hostname:
                params['host'] = url.hostname
            if url.port:
                params['port'] = int(url.port)
            if url.path:
                params['database'] = url.path.lstrip('/')
        else:
            for part in db_dsn.split():
                if '=' in part:
                    k, v = part.split('=', 1)
                    if k == 'dbname':
                        params['database'] = v
                    elif k == 'port':
                        params['port'] = int(v)
                    else:
                        params[k] = v
        
        # Connect and query
        conn = pg8000.connect(**params)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT email, up, down, last_online, expiry_time, total, enable FROM client_traffics;")
            rows_raw = cursor.fetchall()
            rows = []
            for row in rows_raw:
                email = row[0]
                up = int(row[1]) if row[1] is not None else 0
                down = int(row[2]) if row[2] is not None else 0
                last_online = int(row[3]) if row[3] is not None else 0
                expiry_time = int(row[4]) if row[4] is not None else 0
                total = int(row[5]) if row[5] is not None else 0
                
                enable_val = row[6]
                if isinstance(enable_val, bool):
                    enable = 1 if enable_val else 0
                elif isinstance(enable_val, int):
                    enable = 1 if enable_val == 1 else 0
                else:
                    enable = 1 if str(enable_val).strip().lower() in ("1", "t", "true") else 0
                
                rows.append((email, up, down, last_online, expiry_time, total, enable))
            return rows
        finally:
            conn.close()
    except Exception as e_pg8000:
        # 2. Try psycopg2 if pg8000 fails
        try:
            import psycopg2
            conn = psycopg2.connect(db_dsn, connect_timeout=5)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT email, up, down, last_online, expiry_time, total, enable FROM client_traffics;")
                rows_raw = cursor.fetchall()
                rows = []
                for row in rows_raw:
                    email = row[0]
                    up = int(row[1]) if row[1] is not None else 0
                    down = int(row[2]) if row[2] is not None else 0
                    last_online = int(row[3]) if row[3] is not None else 0
                    expiry_time = int(row[4]) if row[4] is not None else 0
                    total = int(row[5]) if row[5] is not None else 0
                    
                    enable_val = row[6]
                    if isinstance(enable_val, bool):
                        enable = 1 if enable_val else 0
                    elif isinstance(enable_val, int):
                        enable = 1 if enable_val == 1 else 0
                    else:
                        enable = 1 if str(enable_val).strip().lower() in ("1", "t", "true") else 0
                        
                    rows.append((email, up, down, last_online, expiry_time, total, enable))
                return rows
            finally:
                conn.close()
        except Exception as e_psycopg2:
            raise RuntimeError(f"Both pg8000 and psycopg2 failed. pg8000 error: {e_pg8000}; psycopg2 error: {e_psycopg2}")

def _v2ray_background_reader():
    """Background thread: reads X-UI data via sqlite3, direct Python Postgres, or psql CLI every 5 seconds.
    Uses the system command-line tools as a fallback to avoid locking and python dependencies."""
    import time as _time
    import subprocess as _sp

    global v2ray_prev_bytes, v2ray_cached_results, v2ray_last_update

    while True:
        db_type, db_dsn = get_xui_db_config()
        query_success = False
        proc = None
        rows = []

        if db_type == "postgres" and db_dsn:
            try:
                rows = query_postgres_direct(db_dsn)
                query_success = True
            except Exception as e:
                print(f"Direct Python Postgres query failed, falling back to psql CLI: {e}")
                try:
                    proc = _sp.run(
                        ["psql", db_dsn, "-t", "-A", "-F", "|",
                         "-c", "SELECT email, up, down, last_online, expiry_time, total, enable FROM client_traffics;"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        query_success = True
                except Exception as e_cli:
                    print(f"Failed to query postgres via psql CLI: {e_cli}")

        if not query_success:
            db_path = XUIPaths.get_db_path()
            if not os.path.exists(db_path):
                _time.sleep(30)
                continue

            try:
                import tempfile, shutil
                temp_db = os.path.join(tempfile.gettempdir(), "xui_monitor_copy.db")
                try:
                    shutil.copy2(db_path, temp_db)
                except Exception:
                    _time.sleep(10)
                    continue

                proc = _sp.run(
                    ["sqlite3", "-separator", "|", temp_db,
                     "SELECT email, up, down, last_online, expiry_time, total, enable FROM client_traffics;"],
                    capture_output=True, text=True, timeout=5
                )

                try:
                    os.remove(temp_db)
                except Exception:
                    pass

                if proc.returncode == 0:
                    query_success = True
                else:
                    _time.sleep(10)
                    continue
            except Exception as e:
                print(f"Error in sqlite fallback: {e}")
                _time.sleep(10)
                continue

        try:
            if not rows and proc is not None:
                for line in proc.stdout.strip().split("\n"):
                    if not line.strip():
                        continue
                    parts = line.split("|")
                    if len(parts) >= 7:
                        try:
                            email = parts[0]
                            up = int(parts[1]) if parts[1] else 0
                            down = int(parts[2]) if parts[2] else 0
                            last_online = int(parts[3]) if parts[3] else 0
                            expiry_time = int(parts[4]) if parts[4] else 0
                            total = int(parts[5]) if parts[5] else 0
                            
                            enable_val = parts[6].strip().lower() if parts[6] else "0"
                            enable = 1 if enable_val in ("1", "t", "true") else 0
                            
                            rows.append((email, up, down, last_online, expiry_time, total, enable))
                        except (ValueError, IndexError):
                            continue

            current_time = _time.time()
            results = []

            with v2ray_lock:
                for row in rows:
                    email, up, down, last_online, expiry_time, total, enable = row
                    if not email:
                        continue

                    history = v2ray_prev_bytes.get(email, [])
                    history.append({"up": up, "down": down, "time": current_time})
                    history = [e for e in history if current_time - e["time"] <= 120]
                    v2ray_prev_bytes[email] = history

                    # Compute speed from oldest to newest data point
                    if len(history) >= 2:
                        oldest = history[0]
                        latest = history[-1]
                        elapsed = latest["time"] - oldest["time"]
                        if elapsed > 2.0:
                            down_speed = max(0.0, (latest["down"] - oldest["down"]) * 8 / (elapsed * 1024 * 1024))
                            up_speed = max(0.0, (latest["up"] - oldest["up"]) * 8 / (elapsed * 1024 * 1024))
                        else:
                            down_speed = 0.0
                            up_speed = 0.0
                    else:
                        down_speed = 0.0
                        up_speed = 0.0

                    # User is online only if actually using bandwidth (>= 50 Kbps)
                    speed_threshold = 50 / 1024  # 50 Kbps in Mbps
                    is_online = (down_speed >= speed_threshold or up_speed >= speed_threshold)

                    if not is_online:
                        down_speed = 0.0
                        up_speed = 0.0

                    results.append({
                        "email": email,
                        "down_speed_mbps": round(down_speed, 3),
                        "up_speed_mbps": round(up_speed, 3),
                        "total_down_gb": round(down / (1024 ** 3), 3),
                        "total_up_gb": round(up / (1024 ** 3), 3),
                        "last_online": last_online,
                        "is_online": is_online,
                        "enable": bool(enable),
                        "total_limit_gb": round(total / (1024 ** 3), 2) if total else 0.0,
                        "expiry_time": expiry_time,
                        "unique_ips": len(v2ray_ip_counts.get(email, set()))
                    })

                results.sort(key=lambda x: x["down_speed_mbps"], reverse=True)
                v2ray_cached_results = results
                v2ray_last_update = _time.time()

        except Exception as e:
            import traceback
            print(f"Error in _v2ray_background_reader: {e}")
            traceback.print_exc()

        _time.sleep(5)

# Start background reader thread
if ENABLE_V2RAY:
    _v2ray_reader_thread = threading.Thread(target=_v2ray_background_reader, daemon=True)
    _v2ray_reader_thread.start()

def _v2ray_ip_counter():
    """Background thread: parses Xray access log every 10 seconds to count
    unique IPs per user from the last 60 seconds of log entries."""
    import time as _time
    import subprocess as _sp
    import re
    from datetime import datetime, timedelta

    log_path = XUIPaths.get_access_log()
    global v2ray_ip_counts

    while True:
        try:
            if not os.path.exists(log_path):
                _time.sleep(30)
                continue

            # Read last 2000 lines (enough for ~60 seconds of traffic)
            proc = _sp.run(
                ["tail", "-2000", log_path],
                capture_output=True, text=True, timeout=5
            )
            if proc.returncode != 0:
                _time.sleep(10)
                continue

            now = datetime.now()
            cutoff = now - timedelta(seconds=60)
            ip_map = {}  # {email: set(ips)}

            for line in proc.stdout.split("\n"):
                if not line or "email:" not in line:
                    continue
                try:
                    # Parse timestamp: 2026/06/09 01:59:58.472928
                    ts_str = line[:23]  # "2026/06/09 01:59:58.47"
                    ts = datetime.strptime(ts_str[:19], "%Y/%m/%d %H:%M:%S")
                    if ts < cutoff:
                        continue

                    # Parse IP: "from 2.147.243.2:0"
                    from_idx = line.find("from ")
                    if from_idx == -1:
                        continue
                    ip_part = line[from_idx + 5:].split(":")[0]
                    if ip_part == "127.0.0.1":
                        continue

                    # Parse email: "email: AliGhajar"
                    email_idx = line.rfind("email: ")
                    if email_idx == -1:
                        continue
                    email = line[email_idx + 7:].strip()
                    if not email:
                        continue

                    if email not in ip_map:
                        ip_map[email] = set()
                    ip_map[email].add(ip_part)
                except Exception:
                    continue

            with v2ray_lock:
                v2ray_ip_counts = ip_map

        except Exception as e:
            import traceback
            print(f"Error in _v2ray_ip_counter: {e}")
            traceback.print_exc()

        _time.sleep(10)

# Start background IP counter thread
if ENABLE_V2RAY:
    _v2ray_ip_thread = threading.Thread(target=_v2ray_ip_counter, daemon=True)
    _v2ray_ip_thread.start()

@app.get("/api/v2ray/users")
def get_v2ray_users(username: str = Depends(get_current_username)):
    """Return cached V2ray user data."""
    if not ENABLE_V2RAY:
        return {"users": [], "last_update": 0.0, "error": "V2ray monitoring is disabled"}
    with v2ray_lock:
        return {
            "users": list(v2ray_cached_results),
            "last_update": v2ray_last_update
        }

@app.post("/api/settings/interval")
def set_refresh_interval(seconds: int = Query(3), username: str = Depends(get_current_username)):
    global UI_REFRESH_INTERVAL
    if seconds in [1, 3, 5, 10, 20]:
        UI_REFRESH_INTERVAL = seconds
        return {"status": "ok", "interval": seconds}
    raise HTTPException(status_code=400, detail="Invalid interval")



# Time range mapping: range_key -> (total_seconds, aggregate_bucket_seconds)
@app.get("/api/metrics")
async def get_metrics(range: str = Query("1h"), seconds: int = Query(None, ge=60, le=7776000), username: str = Depends(get_current_username)):
    """Return time-series metrics for the given range or custom seconds."""
    if seconds is not None:
        total_seconds = seconds
        # Auto-calculate bucket based on duration
        if seconds <= 7200:        bucket = None       # raw
        elif seconds <= 21600:     bucket = 30          # 30s avg
        elif seconds <= 43200:     bucket = 60          # 1m avg
        elif seconds <= 86400:     bucket = 120         # 2m avg
        elif seconds <= 172800:    bucket = 300         # 5m avg
        elif seconds <= 604800:    bucket = 900         # 15m avg
        else:                      bucket = 3600        # 1h avg
    else:
        total_seconds, bucket = RANGE_MAP.get(range, (3600, None))
    cutoff = time.time() - total_seconds

    with get_db() as conn:
        if bucket is None:
            # Return raw data
            rows = conn.execute("""
                SELECT timestamp, cpu_percent, ram_percent, ram_used_gb, ram_total_gb,
                       disk_percent, disk_used_gb, disk_total_gb,
                       net_sent_rate, net_recv_rate, conn_json, extra_json
                FROM metrics
                WHERE timestamp >= ?
                ORDER BY timestamp ASC
            """, (cutoff,)).fetchall()

            data = [{
                "t": row["timestamp"],
                "cpu": row["cpu_percent"],
                "ram": row["ram_percent"],
                "ram_used": row["ram_used_gb"],
                "ram_total": row["ram_total_gb"],
                "disk": row["disk_percent"],
                "disk_used": row["disk_used_gb"],
                "disk_total": row["disk_total_gb"],
                "net_sent": row["net_sent_rate"],
                "net_recv": row["net_recv_rate"],
                "conns": json.loads(row["conn_json"]) if row["conn_json"] else {},
                "extra": json.loads(row["extra_json"]) if row["extra_json"] else {}
            } for row in rows]
        else:
            # Return aggregated data (PostgreSQL optimized join with DISTINCT ON, SQLite standard query)
            dsn = os.environ.get("MONITOR_DB_DSN")
            if dsn:
                rows = conn.execute("""
                    SELECT 
                        agg.bucket_ts,
                        agg.cpu, agg.ram, agg.ram_used, agg.ram_total, agg.disk, agg.disk_used, agg.disk_total, agg.net_sent, agg.net_recv,
                        latest.conn_json, latest.extra_json
                    FROM (
                        SELECT
                            CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                            AVG(cpu_percent) AS cpu,
                            AVG(ram_percent) AS ram,
                            AVG(ram_used_gb) AS ram_used,
                            MAX(ram_total_gb) AS ram_total,
                            AVG(disk_percent) AS disk,
                            AVG(disk_used_gb) AS disk_used,
                            MAX(disk_total_gb) AS disk_total,
                            AVG(net_sent_rate) AS net_sent,
                            AVG(net_recv_rate) AS net_recv
                        FROM metrics
                        WHERE timestamp >= ?
                        GROUP BY bucket_ts
                    ) agg
                    LEFT JOIN (
                        SELECT DISTINCT ON (bucket_ts)
                            CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                            conn_json, extra_json
                        FROM metrics
                        WHERE timestamp >= ?
                        ORDER BY bucket_ts, timestamp DESC
                    ) latest ON agg.bucket_ts = latest.bucket_ts
                    ORDER BY agg.bucket_ts ASC
                """, (bucket, bucket, cutoff, bucket, bucket, cutoff)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT
                        CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts,
                        AVG(cpu_percent) AS cpu,
                        AVG(ram_percent) AS ram,
                        AVG(ram_used_gb) AS ram_used,
                        MAX(ram_total_gb) AS ram_total,
                        AVG(disk_percent) AS disk,
                        AVG(disk_used_gb) AS disk_used,
                        MAX(disk_total_gb) AS disk_total,
                        AVG(net_sent_rate) AS net_sent,
                        AVG(net_recv_rate) AS net_recv,
                        (SELECT m2.conn_json FROM metrics m2
                         WHERE CAST(m2.timestamp / ? AS INTEGER) = CAST(metrics.timestamp / ? AS INTEGER)
                         ORDER BY m2.timestamp DESC LIMIT 1) AS conn_json,
                        (SELECT m2.extra_json FROM metrics m2
                         WHERE CAST(m2.timestamp / ? AS INTEGER) = CAST(metrics.timestamp / ? AS INTEGER)
                         ORDER BY m2.timestamp DESC LIMIT 1) AS extra_json
                    FROM metrics
                    WHERE timestamp >= ?
                    GROUP BY bucket_ts
                    ORDER BY bucket_ts ASC
                """, (bucket, bucket, bucket, bucket, bucket, bucket, cutoff)).fetchall()

            data = [{
                "t": row["bucket_ts"],
                "cpu": round(row["cpu"], 1),
                "ram": round(row["ram"], 1),
                "ram_used": round(row["ram_used"], 2),
                "ram_total": row["ram_total"],
                "disk": round(row["disk"], 1),
                "disk_used": round(row["disk_used"], 2),
                "disk_total": row["disk_total"],
                "net_sent": round(row["net_sent"]),
                "net_recv": round(row["net_recv"]),
                "conns": json.loads(row["conn_json"]) if row["conn_json"] else {},
                "extra": json.loads(row["extra_json"]) if row["extra_json"] else {}
            } for row in rows]

    # Calculate Total Data Transferred in the Range (in GB)
    # Rate is bytes/sec. To get total bytes: sum(rate * interval_seconds)
    interval_sec = bucket if bucket else COLLECT_INTERVAL
    range_sent_sum_bytes = sum(d["net_sent"] for d in data) * interval_sec
    range_recv_sum_bytes = sum(d["net_recv"] for d in data) * interval_sec

    totals = {
        "sent_gb": round(range_sent_sum_bytes / (1024 ** 3), 2),
        "recv_gb": round(range_recv_sum_bytes / (1024 ** 3), 2)
    }

    return JSONResponse(content={"range": range, "data": data, "totals": totals})


# ─── Top Processes ───────────────────────────────────────────────────────────
# (threading already imported at top of file)

_cached_top_procs = []

def _update_top_processes_loop():
    global _cached_top_procs
    cores = psutil.cpu_count() or 1
    while True:
        try:
            processes = []
            for p in psutil.process_iter(['pid', 'name', 'username', 'cpu_percent', 'memory_info']):
                try:
                    info = p.info
                    mem_b = info['memory_info'].rss if info['memory_info'] else 0
                    if mem_b > 1024**3:
                        mem_str = f"{mem_b / 1024**3:.1f}G"
                    else:
                        mem_str = f"{mem_b / 1024**2:.1f}M"

                    if info['cpu_percent'] > 0.0 or mem_b > 0:
                        processes.append({
                            "pid": info['pid'],
                            "name": info['name'],
                            "user": info['username'] or 'unknown',
                            "cpu": min(100.0, round(info['cpu_percent'] / cores, 1)),
                            "mem": mem_str,
                            "_mem_b": mem_b
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            
            # Sort descending by CPU
            processes.sort(key=lambda x: (x['cpu'], x['_mem_b']), reverse=True)
            
            top_20 = processes[:20]
            # remove the hidden sorting key before sending to frontend
            for p in top_20:
                p.pop('_mem_b', None)
                
            _cached_top_procs = top_20
        except Exception as e:
            print("Top processes loop error:", e)
            
        time.sleep(5)

# Start background thread
_top_proc_thread = threading.Thread(target=_update_top_processes_loop, daemon=True)
_top_proc_thread.start()

@app.get("/api/top_processes")
async def get_top_processes(username: str = Depends(get_current_username)):
    """Return top 20 processes by CPU usage, fetched from background thread."""
    return JSONResponse(content=_cached_top_procs)

# ─── SSH Terminal ────────────────────────────────────────────────────────────

@app.websocket("/api/ssh")
async def ssh_terminal(websocket: WebSocket):
    # Important: WebSockets don't natively support Basic Auth headers in browser API.
    # The actual panel is protected, and this websocket requires the panel interaction.
    # We will log the client IP for accountability.
    client_ip = websocket.client.host if websocket.client else "Unknown"
    
    await websocket.accept()
    
    # Wait for the initial auth payload from the frontend
    try:
        auth_msg_str = await websocket.receive_text()
        auth_msg = json.loads(auth_msg_str)
        if auth_msg.get("type") != "auth":
            await websocket.send_text("Authentication failed: Expected auth payload.\r\n")
            await websocket.close()
            return
            
        host = auth_msg.get("host", "").strip()
        port_raw = auth_msg.get("port", 22)
        try:
            port = int(port_raw) if port_raw else 22
        except (ValueError, TypeError):
            port = 22
            
        username = auth_msg.get("username", "").strip()
        password = auth_msg.get("password", "")
        
        if not host or not username:
            await websocket.send_text("Authentication failed: Missing host or username.\r\n")
            await websocket.close()
            return
            
    except Exception as e:
        print(f"[{client_ip}] Invalid auth payload: {e}")
        try:
            await websocket.close()
        except:
            pass
        return
    
    print(f"[{client_ip}] Attempting SSH connection to {username}@{host}:{port}")
    
    try:
        async with asyncssh.connect(host, port=port, username=username, password=password, known_hosts=None) as conn:
            async with conn.create_process(term_type="xterm") as process:
                
                async def recv_from_ws():
                    try:
                        while True:
                            msg_str = await websocket.receive_text()
                            try:
                                msg = json.loads(msg_str)
                                if msg.get("type") == "data":
                                    process.stdin.write(msg.get("data", ""))
                                elif msg.get("type") == "resize":
                                    cols = msg.get("cols", 80)
                                    rows = msg.get("rows", 24)
                                    process.change_terminal_size(cols, rows, 0, 0)
                            except json.JSONDecodeError:
                                pass
                    except WebSocketDisconnect:
                        pass
                    except Exception as e:
                        print(f"WS Recv Error: {e}")
                
                async def send_to_ws():
                    try:
                        while True:
                            # Read up to 4096 characters at a time
                            data = await process.stdout.read(4096)
                            if not data:
                                break
                            await websocket.send_text(data)
                    except Exception as e:
                        print(f"WS Send Error: {e}")
                
                async def send_err_to_ws():
                    try:
                        while True:
                            data = await process.stderr.read(4096)
                            if not data:
                                break
                            await websocket.send_text(data)
                    except Exception as e:
                        pass
                        
                # Wait for any of the tasks to finish
                await asyncio.gather(
                    recv_from_ws(),
                    send_to_ws(),
                    send_err_to_ws(),
                    process.wait()
                )
    except Exception as e:
        try:
            await websocket.send_text(f"\r\nSSH Connection Error: {str(e)}\r\n")
            await websocket.close()
        except:
            pass


# ─── Virtual Browser ─────────────────────────────────────────────────────────

@app.get("/api/browser/status")
async def get_browser_status(username: str = Depends(get_current_username)):
    if not browser_mgr:
        return {"state": "not_installed"}
    return await browser_mgr.get_status()

class BrowserActionPayload(BaseModel):
    action: str
    config: dict = {}

@app.post("/api/browser/action")
async def browser_action(payload: BrowserActionPayload, username: str = Depends(get_current_username)):
    if not browser_mgr:
        raise HTTPException(status_code=500, detail="browser_mgr not loaded")
        
    action = payload.action
    config = payload.config
    
    # Run the action as a background task so we can return immediately and let the frontend listen to the websocket
    async def run_action():
        try:
            if action == "install":
                await browser_mgr.install(config)
            elif action == "uninstall":
                await browser_mgr.uninstall()
            elif action == "start":
                await browser_mgr.start()
            elif action == "stop":
                await browser_mgr.stop()
            elif action == "clear_cache":
                await browser_mgr.clear_cache()
            else:
                browser_mgr._add_log(f"Unknown action: {action}")
        except Exception as e:
            browser_mgr._add_log(f"Error during {action}: {e}")
            
    asyncio.create_task(run_action())
    
    # Return current presumed status immediately
    if action == "uninstall":
        return {"state": "not_installed"}
    elif action in ["start", "install", "clear_cache"]:
        return {"state": "running"}
    else:
        return {"state": "stopped"}

@app.websocket("/api/browser/ws")
async def browser_ws(websocket: WebSocket):
    await websocket.accept()
    if not browser_mgr:
        await websocket.send_text("Browser manager is offline.")
        await websocket.close()
        return
        
    # Create a unique queue for this connection
    q = asyncio.Queue()
    for msg in browser_mgr.log_history:
        q.put_nowait(msg)
        
    browser_mgr.log_queues.append(q)
    try:
        while True:
            # Wait for logs and send them
            msg = await q.get()
            await websocket.send_text(msg)
    except WebSocketDisconnect:
        pass
    finally:
        if q in browser_mgr.log_queues:
            browser_mgr.log_queues.remove(q)

import urllib.request
import urllib.error
import urllib.parse
from fastapi import Response
from fastapi.responses import StreamingResponse
# websockets import removed (unused)

@app.api_route("/browser", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.api_route("/browser/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.api_route("/browser/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def browser_proxy_http(request: Request, response: Response, path: str = ""):
    if path == "websockify":
        return Response(status_code=400)
    
    url = f"http://127.0.0.1:{browser_mgr.container_port}/browser/{path}"
    if request.url.query:
        url += "?" + request.url.query
        
    method = request.method
    headers = dict(request.headers)
    for h in ["host", "connection", "upgrade", "content-length", "x-real-ip", "x-forwarded-for", "accept-encoding"]:
        headers.pop(h.lower(), None)
        headers.pop(h, None)
        
    body = await request.body()
    req = urllib.request.Request(url, data=body if body else None, headers=headers, method=method)
    
    try:
        def fetch():
            return urllib.request.urlopen(req, timeout=10)
        resp = await asyncio.get_event_loop().run_in_executor(None, fetch)
        
        resp_headers = dict(resp.getheaders())
        for h in ["Transfer-Encoding", "Connection", "Content-Encoding"]:
            resp_headers.pop(h, None)
            resp_headers.pop(h.lower(), None)

        content_type = resp_headers.get("Content-Type", resp_headers.get("content-type", ""))
        
        # For HTML/JS responses, read full body and patch KasmVNC checks
        if "text/html" in content_type or "javascript" in content_type:
            body = resp.read()
            resp.close()
            body_text = body.decode("utf-8", errors="ignore")
            
            if "javascript" in content_type:
                # Patch the exact KasmVNC pre-flight check function
                for old, new in [
                    ('window.isSecureContext?typeof window.VideoDecoder>"u"', 'true?false'),
                    ('window.isSecureContext&&navigator.clipboard', 'true&&navigator.clipboard'),
                    ('"https:"!==location.protocol', "false"),
                    ('location.protocol!=="https:"', "false"),
                    ("'https:'!==location.protocol", "false"),
                    ('typeof VideoDecoder>"u"', "false"),
                    ('typeof VideoDecoder==="undefined"', "false"),
                    ('typeof window.VideoDecoder>"u"', "false"),
                ]:
                    body_text = body_text.replace(old, new)
            
            if "text/html" in content_type:
                # Also inject a script to hide any HTTPS error overlays
                https_bypass = """<script>
(function(){
  try{Object.defineProperty(window,'isSecureContext',{get:()=>true,configurable:true})}catch(e){}
  if(!window.VideoDecoder){
    window.VideoDecoder=class{constructor(o){this._o=o}configure(){}decode(c){if(this._o&&this._o.output)try{this._o.output(c)}catch(e){}}flush(){return Promise.resolve()}close(){}reset(){}static isConfigSupported(){return Promise.resolve({supported:false})}};
    window.VideoEncoder=class{constructor(){}configure(){}encode(){}flush(){return Promise.resolve()}close(){}};
    window.EncodedVideoChunk=class{constructor(o){Object.assign(this,o)}};
  }
  var h=function(){
    document.querySelectorAll('div,p,span').forEach(function(el){
      if(el.textContent&&(
        (el.textContent.indexOf('HTTPS')!==-1&&el.textContent.indexOf('secure')!==-1)||
        (el.textContent.indexOf('WebCodecs')!==-1)
      )){
        el.style.display='none';
        if(el.parentElement)el.parentElement.style.display='none';
      }
    });
  };
  setInterval(h,500);
  document.addEventListener('DOMContentLoaded',h);
})();
</script>"""
                body_text = body_text.replace("</head>", https_bypass + "</head>")
            
            resp_headers.pop("Content-Length", None)
            resp_headers.pop("content-length", None)
            mt = "text/html" if "text/html" in content_type else "application/javascript"
            return Response(content=body_text, status_code=resp.status, headers=resp_headers, media_type=mt)
        
        # For all other content types (images, CSS, etc.), stream directly
        def iterfile():
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    yield chunk
            except Exception:
                pass
            finally:
                resp.close()
        return StreamingResponse(iterfile(), status_code=resp.status, headers=resp_headers)
    except urllib.error.HTTPError as e:
        return Response(content=e.read(), status_code=e.code, headers=dict(e.headers))
    except Exception as e:
        html_error = f"""
        <html><body style="background:#0f172a; color:#10b981; font-family:sans-serif; text-align:center; padding:50px;">
        <h2>Virtual Browser is Starting...</h2>
        <p>The container is spinning up. Please wait 5-10 seconds and <b>refresh this page</b>.</p>
        <p style="color:#64748b; font-size:12px;">(Internal details: {str(e)})</p>
        <script>setTimeout(() => location.reload(), 3000);</script>
        </body></html>
        """
        return Response(content=html_error, media_type="text/html", status_code=502)

@app.websocket("/browser/{path:path}")
async def browser_proxy_ws(websocket: WebSocket, path: str):
    await websocket.accept()
    target_url = f"http://127.0.0.1:{browser_mgr.container_port}/browser/{path}"
    if websocket.query_params:
        target_url += "?" + str(websocket.query_params)
        
    print(f"[*] WS Proxy: Request to {target_url}")
    
    # Get the authorization header from the client request to forward
    auth_header = websocket.headers.get("authorization")
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(target_url, headers=headers) as target_ws:
                print("[*] WS Proxy: Target connection established via aiohttp")
                
                async def forward_to_target():
                    try:
                        while True:
                            msg = await websocket.receive()
                            if msg.get("type") == "websocket.disconnect":
                                await target_ws.close()
                                break
                            if "text" in msg:
                                await target_ws.send_str(msg["text"])
                            elif "bytes" in msg:
                                await target_ws.send_bytes(msg["bytes"])
                    except Exception as e:
                        print(f"[*] WS Proxy: Forward-to-target error: {e}")

                async def forward_to_client():
                    try:
                        async for msg in target_ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await websocket.send_text(msg.data)
                            elif msg.type == aiohttp.WSMsgType.BINARY:
                                await websocket.send_bytes(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                                break
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                print(f"[*] WS Proxy: Target WS error: {target_ws.exception()}")
                                break
                    except Exception as e:
                        print(f"[*] WS Proxy: Forward-to-client error: {e}")

                await asyncio.gather(forward_to_target(), forward_to_client())
    except Exception as e:
        print(f"[*] WS Proxy Error: {e}")
    finally:
        print("[*] WS Proxy: Closing connections")
        try:
            await websocket.close()
        except:
            pass


# Mount static files AFTER API routes
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[*] Server Monitor starting on http://0.0.0.0:{PORT}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
