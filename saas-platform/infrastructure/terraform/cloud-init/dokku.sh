#!/bin/bash
# Simple bash script for Dokku server setup

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
  htop \
  ncdu \
  tmux

# Install Docker
curl -fsSL https://get.docker.com | bash
systemctl enable docker
systemctl start docker

# Install Dokku
wget -NP /tmp https://dokku.com/install/${dokku_version}/bootstrap.sh
sudo DOKKU_TAG=${dokku_version} bash /tmp/bootstrap.sh

# Configure Dokku
dokku domains:set-global ${dokku_domain}

# Create a script to add platform server's SSH key when available
cat > /root/add-platform-key.sh <<'KEYEOF'
#!/bin/bash
# This script will be called by the platform server to add its SSH key
# Usage: ssh root@dokku-server 'bash /root/add-platform-key.sh "ssh-ed25519 AAAA..."'
if [ -z "$$1" ]; then
  echo "Usage: $$0 'ssh-key'"
  exit 1
fi
echo "$$1" >> /root/.ssh/authorized_keys
echo "Platform SSH key added successfully"
KEYEOF
chmod +x /root/add-platform-key.sh

# Install Dokku plugins
dokku plugin:install https://github.com/dokku/dokku-postgres.git postgres
dokku plugin:install https://github.com/dokku/dokku-redis.git redis
dokku plugin:install https://github.com/dokku/dokku-letsencrypt.git
dokku letsencrypt:cron-job --add
dokku plugin:install https://github.com/dokku/dokku-resource-limit.git resource-limit

# Configure firewall
ufw --force enable
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8448/tcp
ufw reload

# Configure fail2ban
systemctl enable fail2ban
systemctl start fail2ban

# Add swap
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Optimize sysctl
cat >> /etc/sysctl.conf <<EOF
vm.max_map_count=262144
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=65535
net.ipv4.ip_local_port_range=1024 65535
EOF
sysctl -p

# Add provisioner SSH key
echo "${provisioner_pub_key}" | dokku ssh-keys:add provisioner

# Create dokku-deploy user
useradd -m -s /bin/bash dokku-deploy
usermod -aG docker dokku-deploy
mkdir -p /home/dokku-deploy/.ssh
echo "${provisioner_pub_key}" > /home/dokku-deploy/.ssh/authorized_keys
chmod 700 /home/dokku-deploy/.ssh
chmod 600 /home/dokku-deploy/.ssh/authorized_keys
chown -R dokku-deploy:dokku-deploy /home/dokku-deploy/.ssh
echo "dokku-deploy ALL=(ALL) NOPASSWD: /usr/bin/dokku" >> /etc/sudoers.d/dokku-deploy

# Mount volume (required for production)
DEVICE=$$(ls /dev/disk/by-id/scsi-0HC_Volume_* | head -n1)
if ! blkid $$DEVICE; then
  mkfs.ext4 $$DEVICE
fi
mkdir -p /mnt/dokku-data
mount $$DEVICE /mnt/dokku-data
UUID=$$(blkid -s UUID -o value $$DEVICE)
echo "UUID=$$UUID /mnt/dokku-data ext4 defaults,nofail 0 2" >> /etc/fstab

# Move Docker data to volume
systemctl stop docker
if [ -d /var/lib/docker ]; then
  mv /var/lib/docker /mnt/dokku-data/
fi
ln -s /mnt/dokku-data/docker /var/lib/docker
systemctl start docker

# Move Dokku data to volume
if [ -d /home/dokku ]; then
  mv /home/dokku /mnt/dokku-data/
fi
ln -s /mnt/dokku-data/dokku /home/dokku

# Set Dokku admin password
echo "dokku:${admin_password}" | chpasswd

echo "Dokku setup complete!"
