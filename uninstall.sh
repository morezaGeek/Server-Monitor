#!/bin/bash
# Server Monitor Dashboard - Uninstaller

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}=======================================${NC}"
echo -e "${BLUE}   Server Monitor Dashboard Uninstaller ${NC}"
echo -e "${BLUE}=======================================${NC}"

read -p "Are you sure you want to completely remove the Server Monitor Dashboard? (y/N): " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo -e "${RED}Uninstallation aborted.${NC}"
    exit 0
fi

echo -e "${BLUE}Stopping and disabling systemd service...${NC}"
systemctl stop server-monitor 2>/dev/null || true
systemctl disable server-monitor 2>/dev/null || true
rm -f /etc/systemd/system/server-monitor.service
systemctl daemon-reload

INSTALL_DIR="/opt/server-monitor"
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${BLUE}Removing installation directory ($INSTALL_DIR)...${NC}"
    rm -rf "$INSTALL_DIR"
fi

echo -e "${GREEN}=======================================${NC}"
echo -e "${GREEN}   Uninstallation Complete!            ${NC}"
echo -e "${GREEN}=======================================${NC}"
