# Testing CloudInit Locally

## Method 1: CloudInit Schema Validation (Fastest)

```bash
# Install cloud-init locally
sudo apt-get install cloud-init

# Validate the YAML syntax and schema
cloud-init schema --config-file dokku-v2.yaml
cloud-init schema --config-file platform-v2.yaml

# More detailed validation
cloud-init devel schema --config-file dokku-v2.yaml --annotate
```

## Method 2: Using Multipass (Ubuntu VMs)

```bash
# Install multipass
sudo snap install multipass

# Create a test instance with your cloud-config
multipass launch --name test-dokku --cloud-init dokku-v2.yaml

# Check the instance
multipass shell test-dokku
multipass exec test-dokku -- cloud-init status --long

# Check logs
multipass exec test-dokku -- sudo cat /var/log/cloud-init.log
multipass exec test-dokku -- sudo cat /var/log/cloud-init-output.log

# Clean up
multipass delete test-dokku
multipass purge
```

## Method 3: Using LXD Containers

```bash
# Install LXD
sudo snap install lxd
sudo lxd init --auto

# Launch container with cloud-init
lxc launch ubuntu:22.04 test-dokku --config=user.user-data="$(cat dokku-v2.yaml)"

# Check status
lxc exec test-dokku -- cloud-init status
lxc exec test-dokku -- cat /var/log/cloud-init.log

# Clean up
lxc delete test-dokku --force
```

## Method 4: Docker with cloud-init (Experimental)

```bash
# Create a Dockerfile that simulates cloud-init
cat > Dockerfile.cloudinit <<'EOF'
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y cloud-init systemd
COPY dokku-v2.yaml /var/lib/cloud/seed/nocloud-net/user-data
COPY meta-data /var/lib/cloud/seed/nocloud-net/meta-data
CMD ["/sbin/init"]
EOF

# Create minimal meta-data
echo "instance-id: test-1" > meta-data

# Build and run
docker build -f Dockerfile.cloudinit -t cloudinit-test .
docker run --rm -it --privileged cloudinit-test
```

## Method 5: Vagrant with cloud-init

```ruby
# Vagrantfile
Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"

  config.vm.provider "virtualbox" do |vb|
    vb.memory = "2048"
  end

  # Use cloud-init
  config.vm.provision "shell", inline: <<-SHELL
    cloud-init clean
    cloud-init init
    cloud-init modules --mode=config
    cloud-init modules --mode=final
  SHELL

  # Provide user-data
  config.vm.provision "file", source: "dokku-v2.yaml", destination: "/tmp/user-data"
  config.vm.provision "shell", inline: "sudo mv /tmp/user-data /var/lib/cloud/seed/nocloud-net/"
end
```

## Method 6: Quick YAML Validation Script

```bash
#!/bin/bash
# save as validate-cloud-init.sh

for file in *.yaml; do
  echo "Validating $file..."

  # Check YAML syntax
  python3 -c "import yaml; yaml.safe_load(open('$file'))" 2>/dev/null
  if [ $? -eq 0 ]; then
    echo "  ✓ Valid YAML syntax"
  else
    echo "  ✗ Invalid YAML syntax"
    python3 -c "import yaml; yaml.safe_load(open('$file'))"
  fi

  # Check with cloud-init if available
  if command -v cloud-init &> /dev/null; then
    cloud-init schema --config-file "$file" 2>&1 | grep -q "Valid cloud-config"
    if [ $? -eq 0 ]; then
      echo "  ✓ Valid cloud-config schema"
    else
      echo "  ✗ Invalid cloud-config schema"
      cloud-init schema --config-file "$file"
    fi
  fi

  echo ""
done
```

## Recommended Approach

For fastest iteration, I recommend:

1. **First**: Use `cloud-init schema` for syntax validation
2. **Then**: Use Multipass for quick VM testing
3. **Finally**: Deploy to actual infrastructure

This gives you a fast feedback loop without waiting for Hetzner servers to provision.
