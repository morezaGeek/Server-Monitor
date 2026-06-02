# 📊 Server Monitor Dashboard

A professional, real-time server monitoring dashboard with a high-performance backend and a sleek, glassmorphic frontend. Track your system performance and manage your server directly from your browser.

![Dashboard Preview](static/preview.png)

## ✨ Features

- **Live System Metrics**: Real-time tracking of CPU, RAM, Disk, and Network I/O with smooth animations and theme-adaptive coloring.
- **Per-Core Visualization**: Detailed per-core CPU usage cylinders.
- **Web SSH Terminal**: Fully functional terminal in your browser powered by xterm.js and asyncssh.
- **Multi-Dimensional Benchmark**: High-intensity, sequential real-world benchmark simulating JSON serialization/deserialization, SHA-256 cryptographic hashing, memory slice copying (RAM GB/s), and direct-sync disk write/read throughput (Disk MB/s) with custom ratings and an overall performance score. Handles 1-core systems gracefully by adapting multi-core tests to a single-core sustained stress test.
- **Persistent History**: Stores historical metrics in an optimized SQLite database with theme-tailored solid popover time ranges.
- **Detached Dual Update & Reinstall**: Elegant Orange (Update via GitHub) and Blue (Reinstall locally) pill buttons running inside detached systemd transient scopes (`systemd-run`) surviving panel service stops and auto-reconnecting.
- **GitHub Release Tracking**: Automatically queries public GitHub Releases in the browser and displays a gorgeous pulsing amber notification badge (`New: v1.0.1`) next to the Update button when a newer version is published.
- **🌐 Virtual Browser**: Secure, Docker-based Chromium browser accessible directly from the dashboard.
- **Beautiful UI**: Modern glassmorphic aesthetic with support for multiple premium themes (Dark, Light, Gruvbox, Nord, Cyberpunk, etc.).

## 🚀 Quick Installation

To install and start the dashboard on a fresh Linux server (Debian, Ubuntu, RHEL, Fedora, CentOS, etc.), simply run:

```bash
curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/install.sh | sudo bash
```

## 🐳 Docker Installation (Recommended for Containerized Environments)

If you prefer to run Server Monitor in a fully isolated container while maintaining accurate host-level metric tracking (network interfaces, disk throughput, real-time memory usage, and processes), you can deploy it using Docker and Docker Compose.

To monitor the *host* resources from inside the container, we mount the host's `/proc` and `/sys` filesystems as read-only volumes and share the host's network namespace (`--network host` or `network_mode: host`).

### Option A: Local Build & Run (No Docker Hub pull required)
If you have cloned the repository, you can build and run the image locally:

1. **Build the image locally:**
   ```bash
   docker build -t server-monitor .
   ```

2. **Run the container:**
   ```bash
   docker run -d \
     --name server-monitor \
     --restart always \
     --network host \
     -e PORT=8080 \
     -e PANEL_USERNAME=admin \
     -e PANEL_PASSWORD=admin \
     -e PROCFS_PATH=/host/proc \
     -v /proc:/host/proc:ro \
     -v /sys:/host/sys:ro \
     -v $(pwd)/metrics.db:/app/metrics.db \
     server-monitor
   ```

### Option B: Local Docker Compose (Simplest)
To build and run using our pre-bundled `docker-compose.yml`:
```bash
# Build and start container in the background
docker compose up -d --build
```

### Option C: Remote Pull from Docker Hub
If you want to pull a pre-built image from Docker Hub (if available):
```bash
docker run -d \
  --name server-monitor \
  --restart always \
  --network host \
  -e PORT=8080 \
  -e PANEL_USERNAME=admin \
  -e PANEL_PASSWORD=admin \
  -e PROCFS_PATH=/host/proc \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  -v $(pwd)/metrics.db:/app/metrics.db \
  morezageek/server-monitor:latest
```

### Accessing the Panel
After launching the container, open your browser and navigate to `http://your-server-ip:8080`. Log in with your specified `PANEL_USERNAME` and `PANEL_PASSWORD`.


## 🛠 Manual Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/morezaGeek/Server-Monitor.git
   cd Server-Monitor
   ```

2. **Setup virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run the application**:
   ```bash
   uvicorn app:app --host 0.0.0.0 --port 8080
   ```

## 🔐 Security
The dashboard enforces secure **HTTP Basic Authentication**.
During installation, the script will prompt you to create a secure **Username** and **Password**. 
These credentials are automatically injected into the dashboard's environment via the systemd service.

## ⚙️ Management (Uninstall / Reconfigure / Status)
All management options are built into the same install script. Just run it again:
```bash
curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/install.sh | sudo bash
```
The interactive menu will offer:
1. 🚀 **Install / Update**
2. 🗑️ **Uninstall**
3. ⚙️ **Change Port / Credentials**
4. 📋 **View Service Status**

## 🌐 Virtual Browser

The dashboard includes a **secure, Docker-based virtual Chromium browser** that runs directly on your server.

### How It Works
- A [linuxserver/chromium](https://docs.linuxserver.io/images/docker-chromium) container is deployed **locally** on port `127.0.0.1:3000` (never exposed externally).
- All traffic is proxied through your existing HTTPS connection (port 443).
- Two-layer authentication: **Dashboard login** + **Virtual Browser login**.

### Usage
1. Open the **Virtual Browser** modal from the dashboard.
2. Set a **Username** and **Password** for the browser.
3. Click **Install / Update** — Docker will be installed automatically if needed.
4. Once ready, click **Open Browser Tab** to launch Chromium.

### Requirements
- **Docker** (auto-installed during setup)
- ~1.5 GB disk space for the Chromium image
- Works on **Ubuntu, Debian, AlmaLinux, Fedora, CentOS, RHEL**

## 👨‍💻 Author
Created by **morezaGeek**

## 📄 License
This project is for educational and personal use.
