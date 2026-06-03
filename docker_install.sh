#!/usr/bin/env bash
# ==============================================================================
# Server Monitor Docker One-Click Installer
# ==============================================================================
# This script installs Docker (if missing) and deploys Server Monitor from
# the official Docker Hub image in one single step.
# ==============================================================================

set -e

# Color definitions
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

echo -e "${BLUE}======================================================${NC}"
echo -e "${BLUE}${BOLD}        Server Monitor Docker Installer             ${NC}"
echo -e "${BLUE}======================================================${NC}"

# Check root privilege
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}✗ Error: This script must be run as root. Use sudo.${NC}"
    exit 1
fi

# 1. Check/Install Docker
echo -e "\n${BLUE}▸ Step 1: Checking for Docker dependency...${NC}"
if ! [ -x "$(command -v docker)" ]; then
    echo -e "${YELLOW}ℹ Docker is not installed. Installing Docker via convenience script...${NC}"
    # Remove broken repository lists if any
    rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker*.list
    curl -fsSL https://get.docker.com | sh
    systemctl start docker
    systemctl enable docker
    echo -e "${GREEN}✔ Docker successfully installed!${NC}"
else
    echo -e "${GREEN}✔ Docker is already installed.${NC}"
    systemctl start docker 2>/dev/null || true
fi

# 2. Get User Configurations
echo -e "\n${BLUE}▸ Step 2: Configuring Panel Settings...${NC}"

# Default variables
DEFAULT_PORT="8080"
DEFAULT_USER="admin"
DEFAULT_PASS="admin"

read -e -p "  ?? Dashboard Port [${DEFAULT_PORT}]: " USER_PORT
PANEL_PORT=${USER_PORT:-$DEFAULT_PORT}

read -e -p "  ?? Panel Username [${DEFAULT_USER}]: " USER_USERNAME
PANEL_USERNAME=${USER_USERNAME:-$DEFAULT_USER}

read -e -p "  ?? Panel Password [${DEFAULT_PASS}]: " USER_PASSWORD
PANEL_PASSWORD=${USER_PASSWORD:-$DEFAULT_PASS}

# 3. Initialize Target Directory & Database
echo -e "\n${BLUE}▸ Step 3: Preparing directory structure...${NC}"
TARGET_DIR="/opt/server-monitor"
mkdir -p "$TARGET_DIR"

# Critical step: initialize metrics.db as a FILE, not a folder
if [ -d "$TARGET_DIR/metrics.db" ]; then
    echo -e "${YELLOW}⚠️ Found metrics.db directory. Removing to avoid mount failure...${NC}"
    rm -rf "$TARGET_DIR/metrics.db"
fi
touch "$TARGET_DIR/metrics.db"
echo -e "${GREEN}✔ Database file initialized at $TARGET_DIR/metrics.db${NC}"

# 4. Pull and Start the Container
echo -e "\n${BLUE}▸ Step 4: Launching Server Monitor container...${NC}"

DOCKER_IMAGE="reza13721205/server-monitor:latest"

# Remove existing container if running
docker stop server-monitor 2>/dev/null || true
docker rm server-monitor 2>/dev/null || true

echo -e "${BLUE}▸ Pulling image from Docker Hub: $DOCKER_IMAGE...${NC}"
docker pull "$DOCKER_IMAGE"

echo -e "${BLUE}▸ Starting container...${NC}"
docker run -d \
  --name server-monitor \
  --restart always \
  --network host \
  -e PORT="$PANEL_PORT" \
  -e PANEL_USERNAME="$PANEL_USERNAME" \
  -e PANEL_PASSWORD="$PANEL_PASSWORD" \
  -e PROCFS_PATH=/host/proc \
  -v /proc:/host/proc:ro \
  -v /sys:/host/sys:ro \
  -v "$TARGET_DIR/metrics.db":/app/metrics.db \
  "$DOCKER_IMAGE"

# Verify if it's running
sleep 2
if [ "$(docker inspect -f '{{.State.Running}}' server-monitor 2>/dev/null)" = "true" ]; then
    IP_ADDR=$(hostname -I | awk '{print $1}')
    echo -e "\n${GREEN}======================================================${NC}"
    echo -e "${GREEN}🎉 SUCCESS! Server Monitor deployed successfully!${NC}"
    echo -e "${GREEN}======================================================${NC}"
    echo -e "  🌐 Dashboard URL: ${BOLD}http://$IP_ADDR:$PANEL_PORT${NC}"
    echo -e "  👤 Username:      ${BOLD}$PANEL_USERNAME${NC}"
    echo -e "  🔑 Password:      ${BOLD}$PANEL_PASSWORD${NC}"
    echo -e "${GREEN}======================================================${NC}"
else
    echo -e "\n${RED}✗ Error: Container failed to start. Run 'docker logs server-monitor' to view details.${NC}"
    exit 1
fi
