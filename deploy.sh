#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Server Monitor — Full Deploy Script
# Installs the app + Nginx reverse proxy + SSL for panel.rahanetmci.com
# ═══════════════════════════════════════════════════════════════════════════

set -e

DOMAIN="panel.rahanetmci.com"
INSTALL_DIR="/opt/server-monitor"
SERVICE_NAME="server-monitor"
APP_PORT=8080

echo "============================================"
echo "  Server Monitor — Full Deployment"
echo "============================================"

# Check root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── Step 1: Install Python & Dependencies ─────────────────────────────────

echo ""
echo "[1/6] Installing Python dependencies..."

if command -v dnf &> /dev/null; then
    dnf install -y python3 python3-pip 2>/dev/null || true
elif command -v apt &> /dev/null; then
    apt update && apt install -y python3 python3-pip python3-venv 2>/dev/null || true
fi

echo "Python3: $(python3 --version)"

# ─── Step 2: Install App ── ─────────────────────────────────────────────────

echo ""
echo "[2/6] Installing Server Monitor app..."

mkdir -p "${INSTALL_DIR}"
cp "${SCRIPT_DIR}/app.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/static" "${INSTALL_DIR}/"

# Create venv
python3 -m venv "${INSTALL_DIR}/venv" 2>/dev/null || python3 -m venv "${INSTALL_DIR}/venv" --without-pip
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q 2>/dev/null || true
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

echo "App installed at ${INSTALL_DIR}"

# ─── Step 3: Create systemd service ────────────────────────────────────────

echo ""
echo "[3/6] Setting up systemd service..."

cat > /etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=Server Monitor Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

echo "Service started"

# Wait for app to be ready
sleep 2

# ─── Step 4: Install & Configure Nginx ─────────────────────────────────────

echo ""
echo "[4/6] Configuring Nginx..."

# Install nginx if not present
if ! command -v nginx &> /dev/null; then
    if command -v dnf &> /dev/null; then
        dnf install -y nginx
    elif command -v apt &> /dev/null; then
        apt install -y nginx
    fi
fi

# Create conf.d directory if it doesn't exist
mkdir -p /etc/nginx/conf.d

# Create the site config for panel.rahanetmci.com (HTTP first, SSL later)
cat > /etc/nginx/conf.d/panel.rahanetmci.com.conf <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    # For certbot challenge
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass http://127.0.0.1:${APP_PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}
EOF

# Check if nginx.conf has http block, if not we need to add one
if ! grep -q "http {" /etc/nginx/nginx.conf 2>/dev/null; then
    # The existing nginx.conf only has stream block
    # We need to add an http block that includes conf.d
    
    # Backup current config
    cp /etc/nginx/nginx.conf /etc/nginx/nginx.conf.bak.$(date +%s)
    
    # Read existing content and append http block
    cat >> /etc/nginx/nginx.conf <<EOF

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    sendfile      on;
    tcp_nopush    on;
    keepalive_timeout 65;
    types_hash_max_size 4096;
    server_names_hash_bucket_size 128;

    # Logging
    access_log /var/log/nginx/access.log;
    error_log  /var/log/nginx/error.log;

    # Gzip
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;

    # Include site configs
    include /etc/nginx/conf.d/*.conf;
}
EOF
    echo "Added http block to nginx.conf"
else
    # http block exists, make sure it includes conf.d
    if ! grep -q "include /etc/nginx/conf.d/" /etc/nginx/nginx.conf 2>/dev/null; then
        # Add include directive inside http block
        sed -i '/http {/a\    include /etc/nginx/conf.d/*.conf;' /etc/nginx/nginx.conf
        echo "Added conf.d include to existing http block"
    fi
fi

# Test nginx config
nginx -t

# Restart nginx
systemctl restart nginx

echo "Nginx configured for ${DOMAIN}"

# ─── Step 5: Open Firewall ─────────────────────────────────────────────────

echo ""
echo "[5/6] Configuring firewall..."

if command -v ufw &> /dev/null; then
    ufw allow 80/tcp 2>/dev/null || true
    ufw allow 443/tcp 2>/dev/null || true
    echo "UFW rules added"
elif command -v firewall-cmd &> /dev/null; then
    firewall-cmd --permanent --add-service=http 2>/dev/null || true
    firewall-cmd --permanent --add-service=https 2>/dev/null || true
    firewall-cmd --reload 2>/dev/null || true
    echo "firewalld rules added"
fi

# ─── Step 6: SSL with Certbot ──────────────────────────────────────────────

echo ""
echo "[6/6] Setting up SSL certificate..."

# Install certbot
if ! command -v certbot &> /dev/null; then
    if command -v dnf &> /dev/null; then
        dnf install -y certbot python3-certbot-nginx
    elif command -v apt &> /dev/null; then
        apt install -y certbot python3-certbot-nginx
    fi
fi

# Create webroot directory
mkdir -p /var/www/html

# Get certificate
echo ""
echo "Getting SSL certificate for ${DOMAIN}..."
certbot --nginx -d ${DOMAIN} --non-interactive --agree-tos --register-unsafely-without-email --redirect || {
    echo ""
    echo "WARNING: SSL certificate failed. This might be because:"
    echo "  1. DNS for ${DOMAIN} doesn't point to this server yet"
    echo "  2. Port 80 is not accessible from outside"
    echo ""
    echo "You can try again later with:"
    echo "  certbot --nginx -d ${DOMAIN} --non-interactive --agree-tos --register-unsafely-without-email --redirect"
    echo ""
    echo "For now, the dashboard is available at: http://${DOMAIN}"
}

# ─── Done ──────────────────────────────────────────────────────────────────

echo ""
echo "============================================"
echo "  Deployment Complete!"
echo "============================================"
echo ""
echo "  Dashboard: https://${DOMAIN}"
echo "  Fallback:  http://${DOMAIN}"
echo ""
echo "  Commands:"
echo "    systemctl status ${SERVICE_NAME}"
echo "    journalctl -u ${SERVICE_NAME} -f"
echo "    systemctl restart ${SERVICE_NAME}"
echo ""
