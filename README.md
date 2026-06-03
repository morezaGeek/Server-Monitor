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

---

### Option A: One-Click Installer (Recommended - Easiest)
If you have a fresh server with absolutely nothing installed, this interactive script installs Docker automatically, sets up the database file, configures your port/credentials, and starts the panel in a single step:

```bash
curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/docker_install.sh | sudo bash
```

---

### Option B: Step-by-Step Docker Compose Setup
If you want to manually set up and run Server Monitor using Docker Compose, follow these 3 simple steps.

#### Step 1: Create application directory and database file
Run these commands to prepare the required paths on your host server:
```bash
mkdir -p /opt/server-monitor
touch /opt/server-monitor/metrics.db
```

#### Step 2: Create the config file
To prevent any text formatting or spacing errors (YAML is extremely sensitive to spaces), copy and paste this **entire block** directly into your terminal. It will write the `docker-compose.yml` file perfectly without needing any editors like `nano` or `vim`:

```bash
cat << 'EOF' > /opt/server-monitor/docker-compose.yml
version: '3.8'

services:
  server-monitor:
    image: reza13721205/server-monitor:latest
    container_name: server-monitor
    restart: always
    network_mode: host
    environment:
      - PORT=8080                  # Change this if port 8080 is in use
      - PANEL_USERNAME=admin       # Your login username
      - PANEL_PASSWORD=admin       # Your login password
      - PROCFS_PATH=/host/proc
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /opt/server-monitor/metrics.db:/app/metrics.db
EOF
```

#### Step 3: Run the panel
Run this command to download and start the application:
```bash
cd /opt/server-monitor
docker compose up -d || docker-compose up -d
```

---

### ⚙️ Modifying Port or Credentials Later
If you ever want to change your port or login credentials:

1. Open the file to edit:
   ```bash
   nano /opt/server-monitor/docker-compose.yml
   ```
2. Edit the variables inside the `environment:` section (e.g. change port to `8085`, username to `myuser`, password to `mypassword`).
3. Save the file (`Ctrl+O`, `Enter`, `Ctrl+X`) and run:
   ```bash
   cd /opt/server-monitor
   docker compose up -d || docker-compose up -d
   ```

### Accessing the Panel
Open your browser and navigate to `http://your-server-ip:8080` (or whichever port you configured). Log in using your username and password.

### 💾 Database Persistence & Backups
Server Monitor uses a lightweight SQLite database (`metrics.db`) to record historical charts. By default under Docker, this file is persisted on your host machine to ensure safety during container updates or restarts.

* **Database File Location on Host:** `/opt/server-monitor/metrics.db`
* **Container Rebuild Safety:** You can safely upgrade, stop, or delete your Docker container. Your historical metrics and dashboard statistics are safe and will be automatically reloaded when a new container starts.
* **Creating a Backup:** Since the database is a standard SQLite file, backing it up is as simple as copying it:
  ```bash
  cp /opt/server-monitor/metrics.db /opt/server-monitor/metrics_backup.db
  ```




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
