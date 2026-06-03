# 📊 Server Monitor Dashboard

A professional, real-time server monitoring dashboard with a high-performance backend and a sleek, glassmorphic frontend. Track your system performance and manage your server directly from your browser.

![Dashboard Preview](https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/static/preview.png)

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

If you prefer to run Server Monitor in a fully isolated container while maintaining accurate host-level metric tracking (network interfaces, disk throughput, real-time memory usage, and processes), you can deploy it using Docker.

To monitor the *host* resources from inside the container, we mount the host's `/proc` and `/sys` filesystems as read-only volumes and share the host's network namespace (`--network host` or `network_mode: host`).

### Option A: One-Click Installer (Recommended for Fresh Servers)
If you have a fresh server with nothing installed (no Docker, no repositories cloned), this interactive script will install Docker automatically, initialize files, configure your settings, and start the panel in one step:

```bash
curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/docker_install.sh | sudo bash
```

---

### Option B: Remote Pull from Docker Hub
If you already have Docker installed and want to run a pre-built image directly from Docker Hub without downloading any source code:
```bash
# 1. Initialize an empty metrics file on host to avoid bind mount directory bugs
mkdir -p /opt/server-monitor
touch /opt/server-monitor/metrics.db

# 2. Start the container
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
  -v /opt/server-monitor/metrics.db:/app/metrics.db \
  reza13721205/server-monitor:latest
```

---

### Option C: Local Docker Compose (For Custom Builds)
If you have cloned the repository and want to build the Docker image locally:

1. **Build and run via Docker Compose:**
   ```bash
   docker compose up -d --build
   ```

2. **Alternatively, run via Docker CLI directly:**
   ```bash
   # Build the image locally
   docker build -t server-monitor .

   # Initialize file and run
   mkdir -p /opt/server-monitor && touch /opt/server-monitor/metrics.db
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
     -v /opt/server-monitor/metrics.db:/app/metrics.db \
     server-monitor
   ```

### Accessing the Panel
After launching the container, open your browser and navigate to `http://your-server-ip:8080` (or your configured port). Log in with your specified `PANEL_USERNAME` and `PANEL_PASSWORD`.

### ⚙️ Modifying Port and Credentials in Docker
You do **not** need to rebuild the Docker image to change the dashboard's port or login credentials. All options are controlled via environment variables.

#### For Docker Compose:
1. Open your `docker-compose.yml` file and update the environment variables:
   ```yaml
   environment:
     - PORT=8085              # Change the dashboard port (e.g., 8085)
     - PANEL_USERNAME=myuser  # Change panel username
     - PANEL_PASSWORD=mypassword  # Change panel password
   ```
2. Recreate the container with the new settings (takes 1 second):
   ```bash
   docker compose up -d
   ```

#### For Direct `docker run`:
If running via docker CLI directly, simply stop, remove, and launch a new container with the updated environment options:
```bash
docker stop server-monitor
docker rm server-monitor
docker run -d \
  --name server-monitor \
  --restart always \
  --network host \
  -e PORT=8085 \
  -e PANEL_USERNAME=myuser \
  -e PANEL_PASSWORD=mypassword \
  -e PROCFS_PATH=/host/proc \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  -v $(pwd)/metrics.db:/app/metrics.db \
  reza13721205/server-monitor:latest
```

> [!NOTE]
> Recreating the container will **not** delete your metrics database. Your historical charts are safely persisted in the mounted `metrics.db` on your host machine.



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
