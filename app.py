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
import concurrent.futures
import multiprocessing
import asyncio
import pty
from contextlib import contextmanager, asynccontextmanager

import platform
import re

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
COLLECT_INTERVAL = 30  # seconds
RETENTION_DAYS = 31
PORT = 8080

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    """Initialize the SQLite database."""
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


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ─── Data Collector ──────────────────────────────────────────────────────────

class MetricsCollector:
    """Background collector that samples system metrics every COLLECT_INTERVAL seconds."""

    def __init__(self):
        self._prev_net = None  # Will be set after _get_default_nic_counters is defined
        self._prev_time = time.time()
        self._running = False
        self._thread = None

    def start(self):
        self._prev_net = _get_default_nic_counters()
        self._prev_time = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            try:
                self._collect()
            except Exception as e:
                print(f"[Collector Error] {e}")
            time.sleep(COLLECT_INTERVAL)

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

        # Cap spikes at 400 Mbps (50 MB/s) — NIC maximum
        MAX_RATE = 50_000_000  # 400 Mbps in bytes/sec
        if sent_rate > MAX_RATE or sent_rate < 0:
            sent_rate = 0
        if recv_rate > MAX_RATE or recv_rate < 0:
            recv_rate = 0

        self._prev_net = net
        self._prev_time = now

        # Connections per interface
        conn_data = _get_connection_counts()
        conn_json_str = json.dumps(conn_data)
        
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
            "disk_write_iops": disk_speed_data.get("write_iops", 0)
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

        # Cleanup old data
        cutoff = now - (RETENTION_DAYS * 86400)
        with get_db() as conn:
            conn.execute("DELETE FROM metrics WHERE timestamp < ?", (cutoff,))


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
        with open("/proc/cpuinfo", "r") as f:
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

def _benchmark_worker(duration_seconds: int, return_dict: dict, idx: int):
    """A highly intensive CPU-bound task to hit 100% usage."""
    import os
    try:
        os.nice(15)  # Lower priority so the web server remains responsive to Stop requests
    except AttributeError:
        pass
        
    start = time.time()
    iterations = 0
    while time.time() - start < duration_seconds:
        x = 3.14159
        for _ in range(50000):
            x = (x * 2.71828) / 1.61803
        iterations += 1
    return_dict[idx] = iterations

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
        return_dict = manager.dict()
        
        processes = []
        for i in range(cores):
            p = ctx.Process(target=_benchmark_worker, args=(5, return_dict, i))
            processes.append(p)
            
        with _benchmark_lock:
            _benchmark_processes = processes
            
        for p in processes:
            p.start()
            
        for p in processes:
            p.join()
            
        score = sum(return_dict.values())
        return score

    loop = asyncio.get_running_loop()
    try:
        score = await loop.run_in_executor(None, run_benchmark)
    finally:
        with _benchmark_lock:
            _benchmark_processes = []
            _benchmark_running = False
            
    # Normalize score based on new loop iteration size
    normalized_score = int(score * 14.5) if score > 0 else 0
    
    return {
        "score": normalized_score,
        "cores": cores,
        "duration_sec": 5
    }

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


# ─── Settings & Database Endpoints ───────────────────────────────────────────

class SSLSettings(BaseModel):
    certificate_pem: str
    private_key_pem: str

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


@app.get("/")
async def index(username: str = Depends(get_current_username)):
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/interfaces")
async def list_interfaces(username: str = Depends(get_current_username)):
    """Return list of network interfaces with metadata."""
    return {"default": DEFAULT_NIC, "interfaces": _get_all_nic_info()}


@app.get("/api/current")
async def current_metrics(username: str = Depends(get_current_username)):
    """Return current system metrics snapshot."""
    cpu = psutil.cpu_percent(interval=0.1)
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

    return {
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
            "uptime_seconds": uptime_seconds,
            "load_avg_1m": round(load_avg[0], 2),
            "load_avg_5m": round(load_avg[1], 2),
            "load_avg_15m": round(load_avg[2], 2),
            "os": os_info
        },
        "connections": _get_connection_counts()
    }


# Time range mapping: range_key -> (total_seconds, aggregate_bucket_seconds)
RANGE_MAP = {
    "1h":  (3600,        None),       # raw data
    "2h":  (7200,        None),       # raw data
    "6h":  (21600,       30),         # 30 sec average
    "12h": (43200,       60),         # 1 min average
    "1d":  (86400,       120),        # 2 min average
    "2d":  (172800,      300),        # 5 min average
    "1w":  (604800,      900),        # 15 min average
    "1m":  (2592000,     3600),       # 1 hour average
}


@app.get("/api/metrics")
async def get_metrics(range: str = Query("1h", pattern="^(1h|2h|6h|12h|1d|2d|1w|1m)$"), username: str = Depends(get_current_username)):
    """Return time-series metrics for the given range."""
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
            # Return aggregated data
            rows = conn.execute(f"""
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
import websockets

@app.api_route("/browser", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.api_route("/browser/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.api_route("/browser/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def browser_proxy_http(request: Request, response: Response, path: str = ""):
    if path == "websockify":
        return Response(status_code=400)
    
    url = f"http://127.0.0.1:3000/browser/{path}"
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

        resp_headers = dict(resp.getheaders())
        for h in ["Transfer-Encoding", "Connection", "Content-Encoding"]:
            resp_headers.pop(h, None)
            resp_headers.pop(h.lower(), None)
            
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
    target_url = f"http://127.0.0.1:3000/browser/{path}"
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
