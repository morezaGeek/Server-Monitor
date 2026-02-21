# 📊 Server Monitor Dashboard

A professional, real-time server monitoring dashboard with a high-performance backend and a sleek, glassmorphic frontend. Track your system performance and manage your server directly from your browser.

![Dashboard Preview](https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/static/index.html) *(Note: Replace with actual image URL after upload)*

## ✨ Features

- **Live System Metrics**: Real-time tracking of CPU, RAM, Disk, and Network I/O with smooth animations.
- **Per-Core Visualization**: Detailed per-core CPU usage monitoring.
- **Web SSH Terminal**: Fully functional terminal in your browser powered by xterm.js and asyncssh.
- **CPU Benchmark**: High-intensity multi-core performance testing (with stop functionality).
- **Persistent History**: Stores historical metrics in an optimized SQLite database.
- **Advanced Networking**: Multi-interface support and detailed packet-per-second (PPS) tracking.
- **Beautiful UI**: Modern glassmorphism aesthetic with automatic Dark/Light mode support.

## 🚀 Quick Installation

To install and start the dashboard on a fresh Linux server (Debian, Ubuntu, RHEL, Fedora, CentOS, etc.), simply run:

```bash
curl -sL https://raw.githubusercontent.com/morezaGeek/Server-Monitor/main/install.sh | sudo bash
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
The panel uses **HTTP Basic Authentication**.
- **Default Username**: `root`
- **Default Password**: `16637615Ea@`
*(You can change these credentials inside `app.py` in the `get_current_username` function)*

## 👨‍💻 Author
Created by **morezaGeek**

## 📄 License
This project is for educational and personal use.
