#!/bin/bash
# Server Monitor Dashboard - Universal Management Script
# Compatible with Debian/Ubuntu and RHEL/Fedora/CentOS/AlmaLinux

INSTALL_DIR="/opt/server-monitor"
SERVICE_FILE="/etc/systemd/system/server-monitor.service"
REPO_URL="https://github.com/morezaGeek/Server-Monitor.git"
SSL_DIR="/opt/server-monitor/ssl"

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

detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS=$ID
    else
        echo -e "${RED}✘ Cannot detect OS distribution.${NC}"
        exit 1
    fi
}

install_certbot() {
    if command -v certbot &>/dev/null; then
        return 0
    fi
    echo -e "${BLUE}▸ Installing Certbot...${NC}"
    if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
        apt-get install -y -qq certbot > /dev/null 2>&1
    elif [[ "$OS" == "fedora" || "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "almalinux" || "$OS" == "rocky" ]]; then
        dnf install -y -q certbot > /dev/null 2>&1
    fi
    if command -v certbot &>/dev/null; then
        echo -e "${GREEN}✔ Certbot installed${NC}"
    else
        echo -e "${YELLOW}⚠ Could not install Certbot. SSL features may not work.${NC}"
    fi
}

# Get current SSL config from service file
get_ssl_status() {
    if [ ! -f "$SERVICE_FILE" ]; then
        SSL_ENABLED=false
        return
    fi
    if grep -q "ssl-certfile" "$SERVICE_FILE" 2>/dev/null; then
        SSL_ENABLED=true
        SSL_CERT=$(grep -oP '(?<=--ssl-certfile )\S+' "$SERVICE_FILE" 2>/dev/null)
        SSL_KEY=$(grep -oP '(?<=--ssl-keyfile )\S+' "$SERVICE_FILE" 2>/dev/null)
        SSL_DOMAIN=$(grep -oP '(?<=# SSL_DOMAIN=)\S+' "$SERVICE_FILE" 2>/dev/null)
    else
        SSL_ENABLED=false
    fi
}

# Update/create systemd service with optional SSL
write_service_file() {
    local port=$1
    local user=$2
    local pass=$3
    local cert=$4
    local key=$5
    local domain=$6

    local ssl_args=""
    local domain_comment=""
    if [ -n "$cert" ] && [ -n "$key" ]; then
        ssl_args=" --ssl-keyfile $key --ssl-certfile $cert"
        domain_comment="# SSL_DOMAIN=$domain"
    fi

    cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Server Monitor Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="PANEL_USERNAME=$user"
Environment="PANEL_PASSWORD=$pass"
ExecStart=$INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/venv/bin/uvicorn app:app --host 0.0.0.0 --port $port$ssl_args
Restart=always
RestartSec=3
$domain_comment

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
}

# ─────────────────────────────────────────
# SSL SETUP (called during install or from cert manager)
# ─────────────────────────────────────────
setup_ssl() {
    local port=$1
    local user=$2
    local pass=$3

    echo ""
    divider
    echo -e "${YELLOW}  🔒 HTTPS / SSL Setup${NC}"
    divider
    echo ""
    echo -e "  HTTPS is ${BOLD}required${NC} for the Virtual Browser feature"
    echo -e "  and provides security for your dashboard."
    echo ""
    echo -e "  ${BOLD}1)${NC} I have an existing SSL certificate"
    echo -e "  ${BOLD}2)${NC} Get a free certificate (Let's Encrypt)"
    echo -e "  ${BOLD}0)${NC} Skip — use HTTP only (no Virtual Browser)"
    echo ""
    read -e -p "  Select [0-2]: " SSL_CHOICE < /dev/tty

    case $SSL_CHOICE in
        1)
            echo ""
            read -e -p "  📄 Path to certificate file (fullchain.pem): " CERT_PATH < /dev/tty
            read -e -p "  🔑 Path to private key file (privkey.pem): " KEY_PATH < /dev/tty
            
            if [ ! -f "$CERT_PATH" ]; then
                echo -e "  ${RED}✘ Certificate file not found: $CERT_PATH${NC}"
                return 1
            fi
            if [ ! -f "$KEY_PATH" ]; then
                echo -e "  ${RED}✘ Key file not found: $KEY_PATH${NC}"
                return 1
            fi
            
            # Copy to our SSL dir
            mkdir -p "$SSL_DIR"
            cp "$CERT_PATH" "$SSL_DIR/fullchain.pem"
            cp "$KEY_PATH" "$SSL_DIR/privkey.pem"
            
            write_service_file "$port" "$user" "$pass" "$SSL_DIR/fullchain.pem" "$SSL_DIR/privkey.pem" "custom"
            echo -e "  ${GREEN}✔ SSL configured with your certificate${NC}"
            return 0
            ;;
        2)
            if ! command -v certbot &>/dev/null; then
                echo -e "  ${RED}✘ Certbot is not installed. Cannot proceed.${NC}"
                return 1
            fi
            
            echo ""
            echo -e "  ${YELLOW}⚠ Important:${NC}"
            echo -e "  • Port ${BOLD}80${NC} must be open and not in use"
            echo -e "  • Your domain must point to this server's IP"
            echo ""
            read -e -p "  🌐 Enter your domain name (e.g. monitor.example.com): " DOMAIN < /dev/tty
            
            if [ -z "$DOMAIN" ]; then
                echo -e "  ${RED}✘ Domain cannot be empty.${NC}"
                return 1
            fi
            
            echo ""
            echo -e "${BLUE}▸ Requesting certificate for ${BOLD}$DOMAIN${NC}..."
            
            # Stop anything on port 80 temporarily
            systemctl stop server-monitor 2>/dev/null || true
            
            certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email 2>&1
            
            if [ $? -eq 0 ]; then
                local LE_CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
                local LE_KEY="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
                
                if [ -f "$LE_CERT" ] && [ -f "$LE_KEY" ]; then
                    write_service_file "$port" "$user" "$pass" "$LE_CERT" "$LE_KEY" "$DOMAIN"
                    echo -e "  ${GREEN}✔ SSL certificate obtained for $DOMAIN${NC}"
                    
                    # Setup auto-renewal cron
                    (crontab -l 2>/dev/null | grep -v "certbot renew"; echo "0 3 * * * certbot renew --quiet --deploy-hook 'systemctl restart server-monitor'") | crontab -
                    echo -e "  ${GREEN}✔ Auto-renewal configured (daily at 3 AM)${NC}"
                    return 0
                fi
            fi
            
            echo -e "  ${RED}✘ Failed to obtain certificate. Check that:${NC}"
            echo -e "  ${RED}  • $DOMAIN points to this server${NC}"
            echo -e "  ${RED}  • Port 80 is open${NC}"
            return 1
            ;;
        0|*)
            echo -e "  ${BLUE}Skipping SSL setup. Dashboard will use HTTP.${NC}"
            write_service_file "$port" "$user" "$pass" "" "" ""
            return 0
            ;;
    esac
}

# ─────────────────────────────────────────
# INSTALL FUNCTION
# ─────────────────────────────────────────
do_install() {
    echo ""
    echo -e "${BLUE}▸ Starting installation...${NC}"

    detect_os
    echo -e "${GREEN}✔ Detected OS: $OS${NC}"

    # Install prerequisites (including certbot)
    echo -e "${BLUE}▸ Installing system dependencies...${NC}"
    if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
        apt-get update -qq
        apt-get install -y -qq python3 python3-pip python3-venv sqlite3 git curl wget certbot > /dev/null 2>&1
    elif [[ "$OS" == "fedora" || "$OS" == "centos" || "$OS" == "rhel" || "$OS" == "almalinux" || "$OS" == "rocky" ]]; then
        dnf install -y -q python3 python3-pip sqlite git curl wget certbot > /dev/null 2>&1
    else
        echo -e "${RED}✘ Unsupported OS: $OS${NC}"
        exit 1
    fi
    echo -e "${GREEN}✔ Dependencies installed${NC}"

    # Clone/update
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

    # Python venv
    echo -e "${BLUE}▸ Setting up Python environment...${NC}"
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip -q > /dev/null
    pip install -r requirements.txt -q > /dev/null
    echo -e "${GREEN}✔ Python environment ready${NC}"

    # Configuration
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

    # SSL Setup
    setup_ssl "$PANEL_PORT" "$PANEL_USER" "$PANEL_PASS"
    SSL_RESULT=$?

    # If setup_ssl didn't write the service (user skipped), write without SSL
    if [ $SSL_RESULT -ne 0 ]; then
        write_service_file "$PANEL_PORT" "$PANEL_USER" "$PANEL_PASS" "" "" ""
    fi

    # Start service
    echo ""
    echo -e "${BLUE}▸ Starting service...${NC}"
    systemctl enable server-monitor -q 2>/dev/null
    systemctl restart server-monitor

    # Done
    IP_ADDR=$(hostname -I | awk '{print $1}')
    get_ssl_status

    echo ""
    divider
    echo -e "${GREEN}  ✅ Installation Successful!${NC}"
    divider
    echo ""
    if [ "$SSL_ENABLED" = true ]; then
        local display_domain=${SSL_DOMAIN:-$IP_ADDR}
        echo -e "  🌐 Dashboard:  ${BOLD}https://$display_domain:$PANEL_PORT${NC}"
    else
        echo -e "  🌐 Dashboard:  ${BOLD}http://$IP_ADDR:$PANEL_PORT${NC}"
    fi
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

    CURRENT_PORT=$(grep -oP '(?<=--port )\d+' "$SERVICE_FILE" 2>/dev/null || echo "8080")
    CURRENT_USER=$(grep -oP '(?<=PANEL_USERNAME=)[^"]*' "$SERVICE_FILE" 2>/dev/null || echo "")
    CURRENT_PASS=$(grep -oP '(?<=PANEL_PASSWORD=)[^"]*' "$SERVICE_FILE" 2>/dev/null || echo "")

    get_ssl_status
    local ssl_label="HTTP"
    [ "$SSL_ENABLED" = true ] && ssl_label="HTTPS"

    echo -e "${YELLOW}  Current Configuration:${NC}"
    echo -e "  Port: ${BOLD}$CURRENT_PORT${NC}    User: ${BOLD}$CURRENT_USER${NC}    Mode: ${BOLD}$ssl_label${NC}"
    echo ""
    echo -e "  ${CYAN}Leave blank to keep current value.${NC}"
    echo ""

    read -e -p "  🔌 New Port [$CURRENT_PORT]: " NEW_PORT < /dev/tty
    read -e -p "  👤 New Username [$CURRENT_USER]: " NEW_USER < /dev/tty
    read -e -p "  🔒 New Password [unchanged]: " NEW_PASS < /dev/tty

    FINAL_PORT=${NEW_PORT:-$CURRENT_PORT}
    FINAL_USER=${NEW_USER:-$CURRENT_USER}
    FINAL_PASS=${NEW_PASS:-$CURRENT_PASS}

    if [ "$SSL_ENABLED" = true ]; then
        write_service_file "$FINAL_PORT" "$FINAL_USER" "$FINAL_PASS" "$SSL_CERT" "$SSL_KEY" "$SSL_DOMAIN"
    else
        write_service_file "$FINAL_PORT" "$FINAL_USER" "$FINAL_PASS" "" "" ""
    fi

    systemctl restart server-monitor

    IP_ADDR=$(hostname -I | awk '{print $1}')
    local proto="http"
    [ "$SSL_ENABLED" = true ] && proto="https"
    local display_host=${SSL_DOMAIN:-$IP_ADDR}

    echo ""
    divider
    echo -e "${GREEN}  ✅ Configuration Updated!${NC}"
    divider
    echo ""
    echo -e "  🌐 Dashboard:  ${BOLD}$proto://$display_host:$FINAL_PORT${NC}"
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
    get_ssl_status

    echo -e "${YELLOW}  Service Status:${NC}"
    echo ""
    systemctl status server-monitor --no-pager -l 2>/dev/null || echo -e "${RED}  Service not found.${NC}"
    echo ""
    divider
    local proto="http"
    local display_host=$IP_ADDR
    if [ "$SSL_ENABLED" = true ]; then
        proto="https"
        [ -n "$SSL_DOMAIN" ] && display_host=$SSL_DOMAIN
    fi
    echo -e "  🌐 URL:  ${BOLD}$proto://$display_host:$CURRENT_PORT${NC}"
    echo -e "  🔒 SSL:  ${BOLD}$( [ "$SSL_ENABLED" = true ] && echo "Enabled ($SSL_DOMAIN)" || echo "Disabled" )${NC}"
    divider
    echo ""
}

# ─────────────────────────────────────────
# CERTIFICATE MANAGER
# ─────────────────────────────────────────
do_cert_manager() {
    echo ""
    if [ ! -f "$SERVICE_FILE" ]; then
        echo -e "${RED}✘ Server Monitor is not installed. Install it first.${NC}"
        return
    fi

    detect_os
    install_certbot

    CURRENT_PORT=$(grep -oP '(?<=--port )\d+' "$SERVICE_FILE" 2>/dev/null || echo "8080")
    CURRENT_USER=$(grep -oP '(?<=PANEL_USERNAME=)[^"]*' "$SERVICE_FILE" 2>/dev/null || echo "")
    CURRENT_PASS=$(grep -oP '(?<=PANEL_PASSWORD=)[^"]*' "$SERVICE_FILE" 2>/dev/null || echo "")
    get_ssl_status

    echo -e "${YELLOW}  🔒 Certificate Manager${NC}"
    divider
    echo ""
    if [ "$SSL_ENABLED" = true ]; then
        echo -e "  Current: ${GREEN}HTTPS enabled${NC} (${BOLD}$SSL_DOMAIN${NC})"
    else
        echo -e "  Current: ${RED}HTTP only (no SSL)${NC}"
    fi
    echo ""
    echo -e "  ${BOLD}1)${NC} 🆕 Get new certificate (Let's Encrypt)"
    echo -e "  ${BOLD}2)${NC} 📄 Use existing certificate files"
    echo -e "  ${BOLD}3)${NC} 🔄 Renew current certificate"
    echo -e "  ${BOLD}4)${NC} 🗑️  Revoke & remove certificate"
    echo -e "  ${BOLD}5)${NC} ❌ Remove SSL (switch to HTTP)"
    echo -e "  ${BOLD}0)${NC} ↩️  Back to main menu"
    echo ""

    read -e -p "  Select [0-5]: " CERT_CHOICE < /dev/tty

    case $CERT_CHOICE in
        1) # Get new cert via Let's Encrypt
            if ! command -v certbot &>/dev/null; then
                echo -e "  ${RED}✘ Certbot is not installed.${NC}"
                return
            fi
            echo ""
            echo -e "  ${YELLOW}⚠ Requirements:${NC}"
            echo -e "  • Port ${BOLD}80${NC} must be open and not in use"
            echo -e "  • Domain must already point to this server's IP"
            echo ""
            read -e -p "  🌐 Domain name (e.g. monitor.example.com): " DOMAIN < /dev/tty
            [ -z "$DOMAIN" ] && { echo -e "  ${RED}✘ Empty domain.${NC}"; return; }

            echo -e "${BLUE}▸ Requesting certificate for ${BOLD}$DOMAIN${NC}..."
            systemctl stop server-monitor 2>/dev/null || true

            certbot certonly --standalone -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email

            if [ $? -eq 0 ]; then
                LE_CERT="/etc/letsencrypt/live/$DOMAIN/fullchain.pem"
                LE_KEY="/etc/letsencrypt/live/$DOMAIN/privkey.pem"
                if [ -f "$LE_CERT" ] && [ -f "$LE_KEY" ]; then
                    write_service_file "$CURRENT_PORT" "$CURRENT_USER" "$CURRENT_PASS" "$LE_CERT" "$LE_KEY" "$DOMAIN"
                    systemctl restart server-monitor

                    # Auto-renewal cron
                    (crontab -l 2>/dev/null | grep -v "certbot renew"; echo "0 3 * * * certbot renew --quiet --deploy-hook 'systemctl restart server-monitor'") | crontab -

                    echo ""
                    divider
                    echo -e "${GREEN}  ✅ SSL Certificate Installed!${NC}"
                    divider
                    echo -e "  🌐 ${BOLD}https://$DOMAIN:$CURRENT_PORT${NC}"
                    echo ""
                    return
                fi
            fi
            systemctl start server-monitor 2>/dev/null
            echo -e "  ${RED}✘ Failed. Check domain DNS and port 80.${NC}"
            ;;

        2) # Use existing cert
            echo ""
            read -e -p "  📄 Certificate path (fullchain.pem): " CERT_PATH < /dev/tty
            read -e -p "  🔑 Private key path (privkey.pem): " KEY_PATH < /dev/tty

            [ ! -f "$CERT_PATH" ] && { echo -e "  ${RED}✘ Certificate not found.${NC}"; return; }
            [ ! -f "$KEY_PATH" ] && { echo -e "  ${RED}✘ Key not found.${NC}"; return; }

            mkdir -p "$SSL_DIR"
            cp "$CERT_PATH" "$SSL_DIR/fullchain.pem"
            cp "$KEY_PATH" "$SSL_DIR/privkey.pem"

            read -e -p "  🌐 Domain for this cert (optional): " DOMAIN < /dev/tty

            write_service_file "$CURRENT_PORT" "$CURRENT_USER" "$CURRENT_PASS" "$SSL_DIR/fullchain.pem" "$SSL_DIR/privkey.pem" "${DOMAIN:-custom}"
            systemctl restart server-monitor

            echo ""
            divider
            echo -e "${GREEN}  ✅ SSL Certificate Configured!${NC}"
            divider
            echo ""
            ;;

        3) # Renew
            if ! command -v certbot &>/dev/null; then
                echo -e "  ${RED}✘ Certbot is not installed.${NC}"
                return
            fi
            echo -e "${BLUE}▸ Renewing certificates...${NC}"
            certbot renew --quiet
            systemctl restart server-monitor
            echo -e "${GREEN}✔ Renewal complete. Service restarted.${NC}"
            ;;

        4) # Revoke
            if [ "$SSL_ENABLED" != true ] || [ -z "$SSL_DOMAIN" ] || [ "$SSL_DOMAIN" = "custom" ]; then
                echo -e "  ${YELLOW}⚠ No Let's Encrypt certificate to revoke.${NC}"
                echo -e "  ${YELLOW}  Use option 5 to remove SSL instead.${NC}"
                return
            fi
            read -e -p "  Revoke certificate for $SSL_DOMAIN? (y/N): " CONFIRM < /dev/tty
            if [[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]]; then
                certbot revoke --cert-name "$SSL_DOMAIN" --non-interactive 2>/dev/null
                certbot delete --cert-name "$SSL_DOMAIN" --non-interactive 2>/dev/null

                write_service_file "$CURRENT_PORT" "$CURRENT_USER" "$CURRENT_PASS" "" "" ""
                systemctl restart server-monitor

                echo -e "${GREEN}✔ Certificate revoked. Switched to HTTP.${NC}"
            else
                echo -e "  ${BLUE}Aborted.${NC}"
            fi
            ;;

        5) # Remove SSL
            write_service_file "$CURRENT_PORT" "$CURRENT_USER" "$CURRENT_PASS" "" "" ""
            systemctl restart server-monitor
            rm -rf "$SSL_DIR" 2>/dev/null

            IP_ADDR=$(hostname -I | awk '{print $1}')
            echo ""
            divider
            echo -e "${GREEN}  ✅ SSL Removed! Switched to HTTP.${NC}"
            divider
            echo -e "  🌐 ${BOLD}http://$IP_ADDR:$CURRENT_PORT${NC}"
            echo ""
            ;;

        0|*) return ;;
    esac
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
echo -e "  ${BOLD}5)${NC} 🔒 Certificate Manager"
echo -e "  ${BOLD}0)${NC} ❌ Exit"
echo ""

read -e -p "  Select an option [0-5]: " CHOICE < /dev/tty

case $CHOICE in
    1) do_install ;;
    2) do_uninstall ;;
    3) do_configure ;;
    4) do_status ;;
    5) do_cert_manager ;;
    0) echo -e "\n  ${BLUE}Goodbye!${NC}\n" ;;
    *) echo -e "\n  ${RED}Invalid option.${NC}\n" ;;
esac
