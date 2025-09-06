#!/usr/bin/env python3
"""CloudInit YAML validator with common issue detection"""

import re
import sys
from pathlib import Path

import yaml


def check_yaml_file(filepath):
    """Validate CloudInit YAML file for common issues"""
    print(f"\n{'=' * 60}")
    print(f"Validating: {filepath}")
    print("=" * 60)

    issues = []
    warnings = []

    # Read the file
    with open(filepath) as f:
        content = f.read()

    # Check 1: Basic YAML syntax
    try:
        data = yaml.safe_load(content)
        print("✓ Valid YAML syntax")
    except yaml.YAMLError as e:
        print(f"✗ YAML syntax error: {e}")
        return False

    # Check 2: Required cloud-config header
    if not content.startswith("#cloud-config"):
        issues.append("Missing '#cloud-config' header")
    else:
        print("✓ Has #cloud-config header")

    # Check 3: Check for problematic pipe characters in write_files
    if data.get("write_files"):
        print("\nChecking write_files section...")
        for idx, file_entry in enumerate(data["write_files"]):
            if "content" in file_entry:
                content_str = file_entry["content"]
                # Check for pipes in shell commands (common issue)
                if "|" in content_str and "bash" in content_str:
                    line_with_pipe = next((line for line in content_str.split("\n") if "|" in line), "")
                    warnings.append(
                        f"  File {file_entry.get('path', idx)}: Contains pipe '|' which may cause YAML parsing issues",
                    )
                    warnings.append(f"    Problem line: {line_with_pipe[:80]}...")

    # Check 4: Check runcmd for proper formatting
    if data.get("runcmd"):
        print("\nChecking runcmd section...")
        for idx, cmd in enumerate(data["runcmd"]):
            if not isinstance(cmd, (str, list)):
                issues.append(f"  Command {idx}: Must be string or list, got {type(cmd)}")
            # Check for unescaped special characters
            if isinstance(cmd, str):
                if "${" in cmd and "$${" not in cmd:
                    warnings.append(f"  Command {idx}: Contains unescaped template variable")
                if "%{" in cmd and "%%{" not in cmd:
                    warnings.append(f"  Command {idx}: Contains unescaped percent template")

    # Check 5: Check users section
    if data.get("users"):
        print("\nChecking users section...")
        for user in data["users"]:
            if isinstance(user, dict):
                if "name" in user:
                    print(f"  ✓ User '{user['name']}' defined")
                    if "ssh_authorized_keys" in user:
                        for key in user["ssh_authorized_keys"]:
                            if len(key) > 1000:
                                print(f"    ✓ Has SSH key (length: {len(key)})")

    # Check 6: Check for template variables
    template_vars = re.findall(r"\$\{(\w+)\}", content)
    if template_vars:
        print(f"\nFound {len(set(template_vars))} template variables:")
        for var in sorted(set(template_vars)):
            print(f"  - ${{{var}}}")

    # Check 7: Look for common problematic patterns
    print("\nChecking for common issues...")

    # Check for $$ escaping in write_files
    if "$$" in content:
        print("  ✓ Found escaped shell variables ($$)")

    # Check for %% escaping
    if "%%" in content:
        print("  ✓ Found escaped percent signs (%%)")

    # Report results
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    if issues:
        print("\n❌ ERRORS found:")
        for issue in issues:
            print(f"  - {issue}")
    else:
        print("\n✅ No critical errors found")

    if warnings:
        print("\n⚠️  WARNINGS:")
        for warning in warnings:
            print(f"  - {warning}")

    return len(issues) == 0


def main():
    """Main function"""
    yaml_files = list(Path().glob("*.yaml"))

    if not yaml_files:
        print("No YAML files found in current directory")
        sys.exit(1)

    print("CloudInit YAML Validator")
    print("========================")

    all_valid = True
    for yaml_file in yaml_files:
        if "v2" in yaml_file.name:  # Only check our v2 files
            if not check_yaml_file(yaml_file):
                all_valid = False

    print("\n" + "=" * 60)
    if all_valid:
        print("✅ All files validated successfully!")
    else:
        print("❌ Some files have issues that need fixing")
    print("=" * 60)

    return 0 if all_valid else 1


if __name__ == "__main__":
    sys.exit(main())
