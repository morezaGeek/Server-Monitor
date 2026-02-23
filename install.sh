#!/bin/bash
# Server Monitor Dashboard - Universal Management Script
# Compatible with Debian/Ubuntu and RHEL/Fedora/CentOS/AlmaLinux

INSTALL_DIR="/opt/server-monitor"
SERVICE_FILE="/etc/systemd/system/server-monitor.service"
REPO_URL="https://github.com/morezaGeek/Server-Monitor.git"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

divider() {
    echo -e "${CYAN}═══════════════════════════════════════════${NC}"
}

header() {
    clear
    divider
    echo -e "${CYAN}  ╔═══════════════════════════════════════╗${NC}"
    echo -e "${CYAN}  ║   ${BOLD}📊 Server Monitor Dashboard${NC}${CYAN}         ║${NC}"
    echo -e "${CYAN}  ╚═══════════════════════════════════════╝${NC}"
    divider
}

# ─────────────────────────────────────────
# INSTALL FUNCTION
# ─────────────────────────────────────────
do_install() {
    echo ""
    echo -e "${BLUE}▸ Starting installation...${NC}"

    # 1. Detect OS
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    else
        echo -e "${RED}✘ Cannot detect OS distribution.${NC}"
        exit 1
    fi
    echo -e "${GREEN}✔ Detected OS: $OS${NC}"

    # 2. Install prerequisites
    echo -e "${BLUE}▸ Installing system dependencies...${NC}"
    if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv sqlite3 git curl wget > /dev/null
    elif [[ "$OS" == "fedora" || "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "almalinux" || "$OS" == "rocky" ]]; then
        dnf install -y -q python3 python3-pip sqlite git curl wget > /dev/null
    else
        echo -e "${RED}✘ Unsupported OS: $OS${NC}"
        exit 1
    fi
    echo -e "${GREEN}✔ Dependencies installed${NC}"

    # 3. Setup directory & clone/update
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo -e "${BLUE}▸ Updating existing installation...${NC}"
        cd "$INSTALL_DIR"
        git fetch --all -q
        git reset --hard origin/main -q
    else
        echo -e "${BLUE}▸ Downloading Server Monitor...${NC}"
        mkdir -p "$INSTALL_DIR"
        git clone "$REPO_URL" "$INSTALL_DIR" -q
        cd "$INSTALL_DIR"
    fi
    echo -e "${GREEN}✔ Files downloaded${NC}"

    # 4. Python venv & dependencies
    echo -e "${BLUE}▸ Setting up Python environment...${NC}"
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip -q > /dev/null
    pip install -r requirements.txt -q > /dev/null
    echo -e "${GREEN}✔ Python environment ready${NC}"

    # 5. Configuration
    echo ""
    divider
    echo -e "${YELLOW}  Configuration${NC}"
    divider

    DEFAULT_PORT=8080
    read -e -p "  🔌 Dashboard Port [${DEFAULT_PORT}]: " USER_PORT < /dev/tty
    PANEL_PORT=${USER_PORT:-$DEFAULT_PORT}

    while true; do
        read -e -p "  👤 Username: " PANEL_USER < /dev/tty
        if [ -n "$PANEL_USER" ]; then break; fi
        echo -e "  ${RED}Username cannot be empty.${NC}"
    done

    while true; do
        read -e -p "  🔒 Password: " PANEL_PASS < /dev/tty
        if [ -n "$PANEL_PASS" ]; then break; fi
        echo -e "  ${RED}Password cannot be empty.${NC}"
    done

    # 6. Create systemd service
    echo ""
    echo -e "${BLUE}▸ Creating systemd service...${NC}"
    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Server Monitor Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="PANEL_USERNAME=$PANEL_USER"
Environment="PANEL_PASSWORD=$PANEL_PASS"
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/venv/bin/uvicorn app:app --host 0.0.0.0 --port $PANEL_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable server-monitor -q
    systemctl restart server-monitor

    # 7. Done
    IP_ADDR=$(hostname -I | awk '{print $1}')
    echo ""
    divider
    echo -e "${GREEN}  ✅ Installation Successful!${NC}"
    divider
    echo ""
    echo -e "  🌐 Dashboard:  ${BOLD}http://$IP_ADDR:$PANEL_PORT${NC}"
    echo -e "  👤 Username:   ${BOLD}$PANEL_USER${NC}"
    echo -e "  🔒 Password:   ${BOLD}$PANEL_PASS${NC}"
    echo -e "  📋 Logs:       ${BOLD}journalctl -u server-monitor -f${NC}"
    echo ""
}

# ─────────────────────────────────────────
# UNINSTALL FUNCTION
# ─────────────────────────────────────────
do_uninstall() {
    echo ""
    if [ ! -f "$SERVICE_FILE" ] && [ ! -d "$INSTALL_DIR" ]; then
        echo -e "${YELLOW}⚠ Server Monitor is not installed.${NC}"
        return
    fi

    read -e -p "  Are you sure you want to completely remove Server Monitor? (y/N): " CONFIRM < /dev/tty
    if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
        echo -e "${RED}  Aborted.${NC}"
        return
    fi

    echo -e "${BLUE}▸ Stopping service...${NC}"
    systemctl stop server-monitor 2>/dev/null || true
    systemctl disable server-monitor 2>/dev/null || true
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload

    if [ -d "$INSTALL_DIR" ]; then
        echo -e "${BLUE}▸ Removing files...${NC}"
        rm -rf "$INSTALL_DIR"
    fi

    echo ""
    divider
    echo -e "${GREEN}  ✅ Uninstallation Complete!${NC}"
    divider
    echo ""
}

# ─────────────────────────────────────────
# CONFIGURE FUNCTION
# ─────────────────────────────────────────
do_configure() {
    echo ""
    if [ ! -f "$SERVICE_FILE" ]; then
        echo -e "${RED}✘ Server Monitor is not installed. Install it first.${NC}"
        return
    fi

    # Extract current values
    CURRENT_PORT=$(grep -oP '(?<=--port )\d+' "$SERVICE_FILE" 2>/dev/null || echo "8080")
    CURRENT_USER=$(grep -oP '(?<=PANEL_USERNAME=)[^"]*' "$SERVICE_FILE" 2>/dev/null || echo "")
    CURRENT_PASS=$(grep -oP '(?<=PANEL_PASSWORD=)[^"]*' "$SERVICE_FILE" 2>/dev/null || echo "")

    echo -e "${YELLOW}  Current Configuration:${NC}"
    echo -e "  Port: ${BOLD}$CURRENT_PORT${NC}    User: ${BOLD}$CURRENT_USER${NC}"
    echo ""
    echo -e "  ${CYAN}Leave blank to keep current value.${NC}"
    echo ""

    read -e -p "  🔌 New Port [$CURRENT_PORT]: " NEW_PORT < /dev/tty
    read -e -p "  👤 New Username [$CURRENT_USER]: " NEW_USER < /dev/tty
    read -e -p "  🔒 New Password [unchanged]: " NEW_PASS < /dev/tty

    FINAL_PORT=${NEW_PORT:-$CURRENT_PORT}
    FINAL_USER=${NEW_USER:-$CURRENT_USER}
    FINAL_PASS=${NEW_PASS:-$CURRENT_PASS}

    # Update service file
    python3 -c "
import sys, re

with open(sys.argv[1], 'r') as f:
    c = f.read()

c = re.sub(r'--port \d+', f'--port {sys.argv[2]}', c)
c = re.sub(r'Environment=\"PANEL_USERNAME=[^\"]*\"', f'Environment=\"PANEL_USERNAME={sys.argv[3]}\"', c)
c = re.sub(r'Environment=\"PANEL_PASSWORD=[^\"]*\"', f'Environment=\"PANEL_PASSWORD={sys.argv[4]}\"', c)

with open(sys.argv[1], 'w') as f:
    f.write(c)
" "$SERVICE_FILE" "$FINAL_PORT" "$FINAL_USER" "$FINAL_PASS"

    systemctl daemon-reload
    systemctl restart server-monitor

    IP_ADDR=$(hostname -I | awk '{print $1}')
    echo ""
    divider
    echo -e "${GREEN}  ✅ Configuration Updated!${NC}"
    divider
    echo ""
    echo -e "  🌐 Dashboard:  ${BOLD}http://$IP_ADDR:$FINAL_PORT${NC}"
    echo -e "  👤 Username:   ${BOLD}$FINAL_USER${NC}"
    echo -e "  🔒 Password:   ${BOLD}$FINAL_PASS${NC}"
    echo ""
}

# ─────────────────────────────────────────
# STATUS FUNCTION
# ─────────────────────────────────────────
do_status() {
    echo ""
    if [ ! -f "$SERVICE_FILE" ]; then
        echo -e "${RED}✘ Server Monitor is not installed.${NC}"
        return
    fi

    CURRENT_PORT=$(grep -oP '(?<=--port )\d+' "$SERVICE_FILE" 2>/dev/null || echo "?")
    IP_ADDR=$(hostname -I | awk '{print $1}')
    
    echo -e "${YELLOW}  Service Status:${NC}"
    echo ""
    systemctl status server-monitor --no-pager -l 2>/dev/null || echo -e "${RED}  Service not found.${NC}"
    echo ""
    divider
    echo -e "  🌐 URL: ${BOLD}http://$IP_ADDR:$CURRENT_PORT${NC}"
    divider
    echo ""
}

# ─────────────────────────────────────────
# MAIN MENU
# ─────────────────────────────────────────
header

echo ""
echo -e "  ${BOLD}1)${NC} 🚀 Install / Update"
echo -e "  ${BOLD}2)${NC} 🗑️  Uninstall"
echo -e "  ${BOLD}3)${NC} ⚙️  Change Port / Credentials"
echo -e "  ${BOLD}4)${NC} 📋 View Service Status"
echo -e "  ${BOLD}0)${NC} ❌ Exit"
echo ""

read -e -p "  Select an option [1-4]: " CHOICE < /dev/tty

case $CHOICE in
    1) do_install ;;
    2) do_uninstall ;;
    3) do_configure ;;
    4) do_status ;;
    0) echo -e "\n  ${BLUE}Goodbye!${NC}\n" ;;
    *) echo -e "\n  ${RED}Invalid option.${NC}\n" ;;
esac
