# Nix Setup for Widget Screenshots

## Quick Install

If you have Nix installed, you can get Chromium for screenshots with:

```bash
# Install Chromium
nix-env -iA nixpkgs.chromium

# Or use a temporary shell with Chromium
nix-shell -p chromium
```

## Using the Development Shell

For a complete development environment:

```bash
cd widget
nix-shell

# This will:
# - Install Chromium and all dependencies
# - Set PUPPETEER_EXECUTABLE_PATH to use Nix's Chromium
# - Skip Puppeteer's Chromium download
```

## Alternative: Direct Environment Variable

If you already have Chromium from Nix:

```bash
# Find your Chromium path
which chromium

# Set the environment variable before running screenshot
export PUPPETEER_EXECUTABLE_PATH=$(which chromium)

# Then run the screenshot
cd /path/to/mindroom-2
python take_screenshot.py
```

## Flake Support (Optional)

If you prefer Nix flakes, create `flake.nix`:

```nix
{
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }: {
    devShells.x86_64-linux.default = nixpkgs.legacyPackages.x86_64-linux.mkShell {
      packages = with nixpkgs.legacyPackages.x86_64-linux; [
        chromium
        nodejs_20
        python311
      ];

      shellHook = ''
        export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
        export PUPPETEER_EXECUTABLE_PATH=${nixpkgs.legacyPackages.x86_64-linux.chromium}/bin/chromium
      '';
    };
  };
}
```

Then use: `nix develop`

## Troubleshooting

If screenshots still fail:

1. Check Chromium is accessible:
   ```bash
   chromium --version
   ```

2. Try running Chromium manually:
   ```bash
   chromium --headless --disable-gpu --screenshot=test.png https://google.com
   ```

3. Ensure the environment variable is set:
   ```bash
   echo $PUPPETEER_EXECUTABLE_PATH
   ```

## System-Wide Nix Configuration

Add to your `configuration.nix` or home-manager:

```nix
environment.systemPackages = with pkgs; [
  chromium
];
```
