#!/bin/bash
# Test CloudInit YAML files locally before deployment

set -e

echo "=== CloudInit Local Testing ==="
echo ""

# Function to test a cloud-init file
test_file() {
    local file=$1
    local temp_file="/tmp/$(basename $file .yaml)-rendered.yaml"

    echo "Testing: $file"
    echo "-------------------"

    # First, check raw YAML syntax
    echo -n "1. YAML syntax check: "
    if python3 -c "import yaml; yaml.safe_load(open('$file'))" 2>/dev/null; then
        echo "✓ Valid"
    else
        echo "✗ Invalid"
        python3 -c "import yaml; yaml.safe_load(open('$file'))" 2>&1 | head -5
        return 1
    fi

    # Create a version with sample values for template variables
    echo -n "2. Creating test version with sample values: "
    sed -e 's/${dokku_version}/v0.32.3/g' \
        -e 's/${dokku_domain}/test.example.com/g' \
        -e 's/${admin_password}/TestPass123!/g' \
        -e 's/${domain}/example.com/g' \
        -e 's/${supabase_url}/https:\/\/test.supabase.co/g' \
        -e 's/${supabase_service_key}/test-key/g' \
        -e 's/${stripe_secret_key}/sk_test_123/g' \
        -e 's/${stripe_webhook_secret}/whsec_test/g' \
        -e 's/${dokku_host}/10.0.0.1/g' \
        -e 's/${hcloud_token}/test-token/g' \
        -e 's/${registry}/docker.io\/test/g' \
        -e 's/${arch}/amd64/g' \
        -e 's/${provisioner_pub_key}/ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDtest test@example/g' \
        "$file" > "$temp_file"
    echo "✓ Done"

    # Check if cloud-init is available
    if command -v cloud-init &> /dev/null; then
        echo -n "3. Cloud-init schema validation: "
        if cloud-init schema --config-file "$temp_file" 2>&1 | grep -q "Valid cloud-config"; then
            echo "✓ Valid cloud-config"
        else
            echo "✗ Invalid cloud-config"
            cloud-init schema --config-file "$temp_file" 2>&1 | grep -v "Valid cloud-config" | head -10
        fi
    else
        echo "3. Cloud-init not installed - skipping schema validation"
        echo "   Install with: sudo apt-get install cloud-init"
    fi

    # If multipass is available, offer to test in VM
    if command -v multipass &> /dev/null; then
        echo ""
        echo "4. Multipass available for VM testing"
        echo "   To test in VM: multipass launch --name test-$(basename $file .yaml) --cloud-init $temp_file"
    fi

    echo ""
    rm -f "$temp_file"
}

# Test each YAML file
for file in dokku-v2.yaml platform-v2.yaml; do
    if [ -f "$file" ]; then
        test_file "$file"
    fi
done

echo "=== Testing Complete ==="#
