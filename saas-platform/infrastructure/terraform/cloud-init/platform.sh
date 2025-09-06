#!/bin/bash
# Simple bash script for Platform server setup

# Update system
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y

# Install required packages
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  apt-transport-https \
  ca-certificates \
  curl \
  software-properties-common \
  git \
  ufw \
  fail2ban \
  nginx \
  certbot \
  python3-certbot-nginx \
  nodejs \
  npm \
  postgresql-client \
  redis-tools \
  htop \
  ncdu \
  tmux \
  jq

# Install Docker
curl -fsSL https://get.docker.com | bash
systemctl enable docker
systemctl start docker

# Install Docker Compose
curl -L "https://github.com/docker/compose/releases/download/v${docker_compose_version}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Configure firewall
ufw --force enable
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw reload

# Configure fail2ban
systemctl enable fail2ban
systemctl start fail2ban

# Add swap
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Optimize sysctl
cat >> /etc/sysctl.conf <<EOF
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=65535
net.ipv4.ip_local_port_range=1024 65535
net.ipv4.tcp_tw_reuse=1
EOF
sysctl -p

# Mount volume (required for production)
DEVICE=$$(ls /dev/disk/by-id/scsi-0HC_Volume_* | head -n1)
if ! blkid $$DEVICE; then
  mkfs.ext4 $$DEVICE
fi
mkdir -p /mnt/platform-data
mount $$DEVICE /mnt/platform-data
UUID=$$(blkid -s UUID -o value $$DEVICE)
echo "UUID=$$UUID /mnt/platform-data ext4 defaults,nofail 0 2" >> /etc/fstab

# Create directories for services
mkdir -p /mnt/platform-data/stripe-handler
mkdir -p /mnt/platform-data/dokku-provisioner
mkdir -p /mnt/platform-data/customer-portal
mkdir -p /mnt/platform-data/admin-dashboard
mkdir -p /mnt/platform-data/nginx
mkdir -p /mnt/platform-data/ssl

# Create platform environment file
mkdir -p /opt/platform
cat > /opt/platform/.env <<EOF
NODE_ENV=production
API_URL=https://api.${domain}
APP_URL=https://app.${domain}
ADMIN_URL=https://admin.${domain}
WEBHOOK_URL=https://webhooks.${domain}
SUPABASE_URL=${supabase_url}
SUPABASE_SERVICE_KEY=${supabase_service_key}
STRIPE_SECRET_KEY=${stripe_secret_key}
STRIPE_WEBHOOK_SECRET=${stripe_webhook_secret}
DOKKU_HOST=${dokku_host}
DOKKU_SSH_PORT=22
HCLOUD_TOKEN=${hcloud_token}
EOF

# Generate SSH key for Dokku access
ssh-keygen -t ed25519 -f /opt/platform/dokku-ssh-key -N "" -C "dokku-provisioner@${domain}"
chmod 600 /opt/platform/dokku-ssh-key
# Make readable for container (will be mounted as read-only)
chmod 644 /opt/platform/dokku-ssh-key

# Note: Docker images should be deployed after infrastructure is up
# This is handled by the deploy-all.sh script
# For now, create placeholder containers that will be replaced

cat > /opt/platform/docker-compose.yml <<'DOCKEREOF'
version: '3.8'

services:
  # These will be replaced by deploy-all.sh with actual images
  # Placeholder to ensure ports are reserved
  placeholder:
    image: busybox
    container_name: placeholder
    command: ["sh", "-c", "echo 'Waiting for deployment' && sleep infinity"]
    restart: unless-stopped

networks:
  default:
    name: platform-network
    driver: bridge
DOCKEREOF

# Create simple landing page
mkdir -p /var/www/landing
cat > /var/www/landing/index.html <<EOF
<!DOCTYPE html>
<html>
<head>
  <title>MindRoom - AI Agent Platform</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      line-height: 1.6;
      color: #333;
      max-width: 800px;
      margin: 0 auto;
      padding: 2rem;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    h1 { color: #2563eb; }
    .cta {
      display: inline-block;
      background: #2563eb;
      color: white;
      padding: 0.75rem 2rem;
      text-decoration: none;
      border-radius: 0.375rem;
      margin-top: 1rem;
    }
    .cta:hover { background: #1d4ed8; }
  </style>
</head>
<body>
  <h1>Welcome to MindRoom</h1>
  <p>The AI agent platform that adapts to your needs.</p>
  <a href="https://app.${domain}" class="cta">Get Started</a>
</body>
</html>
EOF

# Configure Nginx default site
cat > /etc/nginx/sites-available/default <<EOF
server {
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name ${domain} www.${domain};

  location / {
    root /var/www/landing;
    index index.html;
    try_files \$uri \$uri/ /index.html;
  }
}
EOF

# Configure platform services (will be activated after deployment)
cat > /etc/nginx/sites-available/platform-services <<EOF
# Customer Portal
server {
  listen 80;
  listen [::]:80;
  server_name app.${domain};

  location / {
    proxy_pass http://localhost:3000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host \$host;
    proxy_cache_bypass \$http_upgrade;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}

# Admin Dashboard
server {
  listen 80;
  listen [::]:80;
  server_name admin.${domain};

  location / {
    proxy_pass http://localhost:3001;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host \$host;
    proxy_cache_bypass \$http_upgrade;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}

# API and Webhooks
server {
  listen 80;
  listen [::]:80;
  server_name api.${domain} webhooks.${domain};

  location / {
    proxy_pass http://localhost:3002;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host \$host;
    proxy_cache_bypass \$http_upgrade;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    # For Stripe webhooks
    proxy_request_buffering off;
    client_max_body_size 10m;
  }

  location /provision {
    proxy_pass http://localhost:3003;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF

# Enable the platform services config
ln -sf /etc/nginx/sites-available/platform-services /etc/nginx/sites-enabled/

# Enable and restart nginx
systemctl enable nginx
systemctl restart nginx

# Create admin htpasswd
echo "admin:$$(openssl passwd -apr1 'MindRoom2024!')" > /etc/nginx/.htpasswd

# Create deployment script that will be run after Docker images are available
cat > /opt/platform/deploy-services.sh <<'DEPLOYEOF'
#!/bin/bash
# This script deploys the actual platform services
# It should be run after the Docker images are available

REGISTRY="$${REGISTRY:-git.nijho.lt/basnijholt}"
ARCH="$${DOCKER_ARCH:-amd64}"

# Stop any placeholder containers
docker stop placeholder 2>/dev/null || true
docker rm placeholder 2>/dev/null || true

# Remove any existing service containers
docker stop customer-portal admin-dashboard stripe-handler dokku-provisioner 2>/dev/null || true
docker rm customer-portal admin-dashboard stripe-handler dokku-provisioner 2>/dev/null || true

# Deploy Customer Portal
docker run -d \
  --name customer-portal \
  --restart always \
  -p 3000:3000 \
  --env-file /opt/platform/.env \
  $${REGISTRY}/customer-portal:$${ARCH}

# Deploy Admin Dashboard (nginx serves on port 80 internally)
docker run -d \
  --name admin-dashboard \
  --restart always \
  -p 3001:80 \
  --env-file /opt/platform/.env \
  $${REGISTRY}/admin-dashboard:$${ARCH}

# Deploy Stripe Handler (service runs on port 3005 internally)
docker run -d \
  --name stripe-handler \
  --restart always \
  -p 3002:3005 \
  --env-file /opt/platform/.env \
  $${REGISTRY}/stripe-handler:$${ARCH}

# Deploy Dokku Provisioner (service runs on port 8002 internally)
docker run -d \
  --name dokku-provisioner \
  --restart always \
  -p 3003:8002 \
  --env-file /opt/platform/.env \
  -e DOKKU_SSH_HOST=${dokku_host} \
  -e DOKKU_SSH_USER=root \
  -e DOKKU_SSH_KEY_PATH=/ssh/id_rsa \
  -v /opt/platform/dokku-ssh-key:/ssh/id_rsa:ro \
  $${REGISTRY}/dokku-provisioner:$${ARCH}

echo "Services deployed successfully!"
docker ps
DEPLOYEOF

chmod +x /opt/platform/deploy-services.sh

# Create a systemd service to ensure containers start on boot
cat > /etc/systemd/system/platform-services.service <<EOF
[Unit]
Description=MindRoom Platform Services
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'docker ps | grep -q "customer-portal\\|admin-dashboard\\|stripe-handler\\|dokku-provisioner" || /opt/platform/deploy-services.sh'
ExecStop=/bin/bash -c 'docker stop customer-portal admin-dashboard stripe-handler dokku-provisioner'

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable platform-services

# Start placeholder container to reserve ports
cd /opt/platform
docker-compose up -d

# Schedule SSL renewal
systemctl enable certbot.timer
systemctl start certbot.timer

# Create a status check script
cat > /opt/platform/check-status.sh <<'STATUSEOF'
#!/bin/bash
echo "=== Platform Services Status ==="
echo "Customer Portal: $$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000)"
echo "Admin Dashboard: $$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3001)"
echo "Stripe Handler: $$(curl -s http://localhost:3002/health | jq -r .status 2>/dev/null || echo "not running")"
echo "Dokku Provisioner: $$(curl -s http://localhost:3003/health | jq -r .status 2>/dev/null || echo "not running")"
echo ""
echo "=== Docker Containers ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
STATUSEOF
chmod +x /opt/platform/check-status.sh

echo "Platform setup complete!"
echo "Note: Run /opt/platform/deploy-services.sh after Docker images are available"
