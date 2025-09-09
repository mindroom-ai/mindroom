#!/bin/bash

# Script to rename all API endpoints across the codebase
# This will update Python, TypeScript, JavaScript, and Markdown files

echo "Starting API endpoint renaming..."
echo "================================"

# Define the base directory (current directory or specify path)
BASE_DIR="${1:-.}"

# Function to perform replacements
replace_in_files() {
    local pattern="$1"
    local replacement="$2"
    local file_types="$3"

    echo "Replacing: $pattern → $replacement"

    # Use find with multiple name patterns
    find "$BASE_DIR" -type f \( $file_types \) -exec sed -i "s|${pattern}|${replacement}|g" {} +
}

# File type patterns
FILE_PATTERNS="-name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.js' -o -name '*.jsx' -o -name '*.md'"

echo ""
echo "1. Renaming User Account endpoints..."
echo "--------------------------------------"
replace_in_files "/api/v1/account/current" "/my/account" "$FILE_PATTERNS"
replace_in_files "/api/v1/account/is-admin" "/my/account/admin-status" "$FILE_PATTERNS"
replace_in_files "/api/v1/account/setup" "/my/account/setup" "$FILE_PATTERNS"

echo ""
echo "2. Renaming User Subscription & Usage endpoints..."
echo "---------------------------------------------------"
replace_in_files "/api/v1/subscription" "/my/subscription" "$FILE_PATTERNS"
replace_in_files "/api/v1/usage" "/my/usage" "$FILE_PATTERNS"

echo ""
echo "3. Renaming User Instance Management endpoints..."
echo "--------------------------------------------------"
# Be careful with the order here to avoid double replacements
# First do the specific endpoints, then the general one
replace_in_files "/api/v1/instances/provision" "/my/instances/provision" "$FILE_PATTERNS"
replace_in_files "/api/v1/instances/{instance_id}/start" "/my/instances/{instance_id}/start" "$FILE_PATTERNS"
replace_in_files "/api/v1/instances/{instance_id}/stop" "/my/instances/{instance_id}/stop" "$FILE_PATTERNS"
replace_in_files "/api/v1/instances/{instance_id}/restart" "/my/instances/{instance_id}/restart" "$FILE_PATTERNS"
# Also handle the variable syntax used in actual code
replace_in_files '/api/v1/instances/\${instance_id}/start' '/my/instances/${instance_id}/start' "$FILE_PATTERNS"
replace_in_files '/api/v1/instances/\${instance_id}/stop' '/my/instances/${instance_id}/stop' "$FILE_PATTERNS"
replace_in_files '/api/v1/instances/\${instance_id}/restart' '/my/instances/${instance_id}/restart' "$FILE_PATTERNS"
replace_in_files '/api/v1/instances/\$\{instanceId\}/start' '/my/instances/${instanceId}/start' "$FILE_PATTERNS"
replace_in_files '/api/v1/instances/\$\{instanceId\}/stop' '/my/instances/${instanceId}/stop' "$FILE_PATTERNS"
replace_in_files '/api/v1/instances/\$\{instanceId\}/restart' '/my/instances/${instanceId}/restart' "$FILE_PATTERNS"
# Handle template literals with backticks
replace_in_files '`/api/v1/instances/\${' '`/my/instances/${' "$FILE_PATTERNS"
# Now do the general instances endpoint
replace_in_files "/api/v1/instances" "/my/instances" "$FILE_PATTERNS"

echo ""
echo "4. Renaming System/Provisioner endpoints..."
echo "--------------------------------------------"
replace_in_files "/api/v1/provision" "/system/provision" "$FILE_PATTERNS"
replace_in_files "/api/v1/sync-instances" "/system/sync-instances" "$FILE_PATTERNS"
# Handle the system endpoints with instance_id
replace_in_files "/api/v1/start/{instance_id}" "/system/instances/{instance_id}/start" "$FILE_PATTERNS"
replace_in_files "/api/v1/stop/{instance_id}" "/system/instances/{instance_id}/stop" "$FILE_PATTERNS"
replace_in_files "/api/v1/restart/{instance_id}" "/system/instances/{instance_id}/restart" "$FILE_PATTERNS"
replace_in_files "/api/v1/uninstall/{instance_id}" "/system/instances/{instance_id}/uninstall" "$FILE_PATTERNS"
# Also handle actual variable syntax
replace_in_files '/api/v1/start/\${instance_id}' '/system/instances/${instance_id}/start' "$FILE_PATTERNS"
replace_in_files '/api/v1/stop/\${instance_id}' '/system/instances/${instance_id}/stop' "$FILE_PATTERNS"
replace_in_files '/api/v1/restart/\${instance_id}' '/system/instances/${instance_id}/restart' "$FILE_PATTERNS"
replace_in_files '/api/v1/uninstall/\${instance_id}' '/system/instances/${instance_id}/uninstall' "$FILE_PATTERNS"
# Handle Python f-strings
replace_in_files '"/api/v1/start/{instance_id}"' '"/system/instances/{instance_id}/start"' "$FILE_PATTERNS"
replace_in_files '"/api/v1/stop/{instance_id}"' '"/system/instances/{instance_id}/stop"' "$FILE_PATTERNS"
replace_in_files '"/api/v1/restart/{instance_id}"' '"/system/instances/{instance_id}/restart"' "$FILE_PATTERNS"
replace_in_files '"/api/v1/uninstall/{instance_id}"' '"/system/instances/{instance_id}/uninstall"' "$FILE_PATTERNS"
# Handle without quotes too
replace_in_files 'f"/api/v1/start/{' 'f"/system/instances/{' "$FILE_PATTERNS"
replace_in_files 'f"/api/v1/stop/{' 'f"/system/instances/{' "$FILE_PATTERNS"
replace_in_files 'f"/api/v1/restart/{' 'f"/system/instances/{' "$FILE_PATTERNS"
replace_in_files 'f"/api/v1/uninstall/{' 'f"/system/instances/{' "$FILE_PATTERNS"

echo ""
echo "5. Renaming Admin endpoints..."
echo "-------------------------------"
replace_in_files "/api/admin/stats" "/admin/stats" "$FILE_PATTERNS"
replace_in_files "/api/admin/instances/{instance_id}/restart" "/admin/instances/{instance_id}/restart" "$FILE_PATTERNS"
replace_in_files "/api/admin/accounts/{account_id}/status" "/admin/accounts/{account_id}/status" "$FILE_PATTERNS"
replace_in_files "/api/admin/auth/logout" "/admin/auth/logout" "$FILE_PATTERNS"
replace_in_files "/api/admin/metrics/dashboard" "/admin/metrics/dashboard" "$FILE_PATTERNS"
# Handle generic admin endpoints with resources
replace_in_files "/api/admin/{resource}/{resource_id}" "/admin/resources/{resource}/{resource_id}" "$FILE_PATTERNS"
replace_in_files "/api/admin/{resource}" "/admin/resources/{resource}" "$FILE_PATTERNS"
# Also handle variable syntax
replace_in_files '/api/admin/\${' '/admin/resources/${' "$FILE_PATTERNS"
# Handle any remaining /api/admin/ patterns
replace_in_files "/api/admin/" "/admin/" "$FILE_PATTERNS"

echo ""
echo "6. Renaming Stripe endpoints..."
echo "--------------------------------"
replace_in_files "/api/v1/stripe/checkout" "/stripe/checkout" "$FILE_PATTERNS"
replace_in_files "/api/v1/stripe/portal" "/stripe/portal" "$FILE_PATTERNS"
replace_in_files "/api/v1/stripe/" "/stripe/" "$FILE_PATTERNS"

echo ""
echo "7. Handling special cases..."
echo "-----------------------------"
# Handle EXPOSED_ENDPOINTS list in main.py
replace_in_files '"/api/v1/subscription"' '"/my/subscription"' "$FILE_PATTERNS"
replace_in_files '"/api/v1/usage"' '"/my/usage"' "$FILE_PATTERNS"
replace_in_files '"/api/v1/account/is-admin"' '"/my/account/admin-status"' "$FILE_PATTERNS"
replace_in_files '"/api/admin/stats"' '"/admin/stats"' "$FILE_PATTERNS"

echo ""
echo "================================"
echo "Endpoint renaming complete!"
echo ""
echo "Files modified:"
echo "- Python files (*.py)"
echo "- TypeScript files (*.ts, *.tsx)"
echo "- JavaScript files (*.js, *.jsx)"
echo "- Markdown files (*.md)"
echo ""
echo "Next steps:"
echo "1. Review the changes with: git diff"
echo "2. Run your tests to ensure everything works"
echo "3. Update any environment variables or config files if needed"
echo "4. Commit the changes"
