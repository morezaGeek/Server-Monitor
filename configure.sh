#!/bin/bash
# Server Monitor Dashboard - Reconfiguration Script

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=======================================${NC}"
echo -e "${BLUE}   Server Monitor Reconfiguration      ${NC}"
echo -e "${BLUE}=======================================${NC}"

INSTALL_DIR="/opt/server-monitor"
SERVICE_FILE="/etc/systemd/system/server-monitor.service"

if [ ! -f "$SERVICE_FILE" ]; then
    echo -e "${RED}Error: Dashboard is not installed at $SERVICE_FILE.${NC}"
    exit 1
fi

# Extract current values
CURRENT_PORT=$(grep -oP '(?<=--port )\d+' "$SERVICE_FILE" || echo "")
CURRENT_USER=$(grep -oP '(?<=PANEL_USERNAME=)[^"]*' "$SERVICE_FILE" || echo "")
CURRENT_PASS=$(grep -oP '(?<=PANEL_PASSWORD=)[^"]*' "$SERVICE_FILE" || echo "")

echo -e "Leave fields blank to keep current values."
read -p "Enter new Dashboard Port [$CURRENT_PORT]: " NEW_PORT < /dev/tty
read -p "Enter new Username [$CURRENT_USER]: " NEW_USER < /dev/tty
read -p "Enter new Password [Keep current]: " NEW_PASS < /dev/tty

FINAL_PORT=${NEW_PORT:-$CURRENT_PORT}
FINAL_USER=${NEW_USER:-$CURRENT_USER}
FINAL_PASS=${NEW_PASS:-$CURRENT_PASS}

# Update service file using Python to handle special characters safely
python3 -c "
import sys
import re

service_file = sys.argv[1]
port = sys.argv[2]
user = sys.argv[3]
pwd = sys.argv[4]

with open(service_file, 'r') as f:
    content = f.read()

content = re.sub(r'--port \d+', f'--port {port}', content)
content = re.sub(r'Environment=\"PANEL_USERNAME=[^\"]*\"', f'Environment=\"PANEL_USERNAME={user}\"', content)
content = re.sub(r'Environment=\"PANEL_PASSWORD=[^\"]*\"', f'Environment=\"PANEL_PASSWORD={pwd}\"', content)

with open(service_file, 'w') as f:
    f.write(content)
" "$SERVICE_FILE" "$FINAL_PORT" "$FINAL_USER" "$FINAL_PASS"

echo -e "${BLUE}Reloading systemd and restarting service...${NC}"
systemctl daemon-reload
systemctl restart server-monitor

IP_ADDR=$(hostname -I | awk '{print $1}')
echo -e "${GREEN}Configuration updated successfully!${NC}"
echo -e "Panel is running at: ${BLUE}http://$IP_ADDR:$FINAL_PORT${NC}"
echo -e "Username: ${BLUE}$FINAL_USER${NC}"
echo -e "Password: ${BLUE}$FINAL_PASS${NC}"
