#!/bin/bash
# Server Monitor Dashboard - Universal Installer
# Compatible with Debian/Ubuntu and RHEL/Fedora/CentOS

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=======================================${NC}"
echo -e "${BLUE}   Server Monitor Dashboard Installer   ${NC}"
echo -e "${BLUE}=======================================${NC}"

# 1. Detect OS
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    VER=$VERSION_ID
else
    echo -e "${RED}Error: Cannot detect OS distribution.${NC}"
    exit 1
fi

echo -e "${GREEN}Detected OS: $OS $VER${NC}"

# 2. Install Prerequisites
echo -e "${BLUE}Installing system dependencies...${NC}"
if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
    apt-get update
    apt-get install -y python3 python3-pip python3-venv sqlite3 git curl wget
elif [[ "$OS" == "fedora" || "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "almalinux" || "$OS" == "rocky" ]]; then
    dnf install -y python3 python3-pip sqlite git curl wget
else
    echo -e "${RED}Unsupported OS: $OS. Please install dependencies manually.${NC}"
    exit 1
fi

# 3. Setup Installation Directory
INSTALL_DIR="/opt/server-monitor"
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${BLUE}Existing installation found at $INSTALL_DIR. Updating...${NC}"
else
    echo -e "${BLUE}Creating installation directory at $INSTALL_DIR...${NC}"
    mkdir -p "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# 4. Clone/Update Repository
if [ -d ".git" ]; then
    git fetch --all
    git reset --hard origin/main
else
    echo -e "${BLUE}Cloning repository...${NC}"
    git clone https://github.com/morezaGeek/Server-Monitor.git .
fi

# 5. Setup Python Virtual Environment
echo -e "${BLUE}Setting up Python virtual environment...${NC}"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 6. Configuration (Port)
DEFAULT_PORT=8080
echo -e "${BLUE}Configuration:${NC}"
read -p "Enter the port for the dashboard [Default: $DEFAULT_PORT]: " USER_PORT
PANEL_PORT=${USER_PORT:-$DEFAULT_PORT}

# 7. Create/Update Systemd Service
echo -e "${BLUE}Configuring systemd service...${NC}"
cat <<EOF > /etc/systemd/system/server-monitor.service
[Unit]
Description=Server Monitor Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/venv/bin/uvicorn app:app --host 0.0.0.0 --port $PANEL_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# 8. Start Service
echo -e "${BLUE}Reloading systemd and starting service...${NC}"
systemctl daemon-reload
systemctl enable server-monitor
systemctl restart server-monitor

# 9. Final Output
IP_ADDR=$(hostname -I | awk '{print $1}')
echo -e "${GREEN}=======================================${NC}"
echo -e "${GREEN}   Installation Successful!            ${NC}"
echo -e "${GREEN}=======================================${NC}"
echo -e "Panel is running at: ${BLUE}http://$IP_ADDR:$PANEL_PORT${NC}"
echo -e "Default Credentials: ${BLUE}root / 16637615Ea@${NC}"
echo -e "View logs: ${BLUE}journalctl -u server-monitor -f${NC}"
