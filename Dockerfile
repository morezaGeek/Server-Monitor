# Use official lightweight Python 3.10 image
FROM python:3.10-slim

# Set environment variables for non-interactive installs and production
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    PANEL_USERNAME=admin \
    PANEL_PASSWORD=admin \
    PROCFS_PATH=/host/proc

# Set the working directory inside the container
WORKDIR /app

# Install system compilation dependencies for psutil and network diagnostics
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    sqlite3 \
    libsqlite3-dev \
    curl \
    ca-certificates \
    iptables \
    ipset \
    dnsmasq \
    && rm -rf /var/lib/apt/lists/*

# Pre-configure dnsmasq to avoid port 53 conflicts before app.py writes to it
RUN echo "port=5353" > /etc/dnsmasq.conf && \
    echo "listen-address=127.0.0.1" >> /etc/dnsmasq.conf && \
    echo "bind-interfaces" >> /etc/dnsmasq.conf

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy all application files to the container
COPY . .

# Expose default dashboard port
EXPOSE 8080

# Run uvicorn server mapping the custom PORT env
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
