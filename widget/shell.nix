{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    # Chromium for Puppeteer
    chromium

    # Required libraries
    glib
    nss
    nspr
    atk
    cups
    dbus
    libdrm
    xorg.libXcomposite
    xorg.libXdamage
    xorg.libXext
    xorg.libXfixes
    xorg.libXrandr
    xorg.libxcb
    expat
    alsa-lib
    pango
    cairo
    at-spi2-atk
    at-spi2-core

    # Node.js and pnpm for running the widget
    nodejs_20
    pnpm

    # Python for backend
    python311

    # uv for Python package management
    uv
  ];

  shellHook = ''
    echo "MindRoom Widget Development Shell"
    echo "Tools available: uv, pnpm, nodejs, python3, chromium"
    echo "Chromium available for screenshots"
    export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
    export PUPPETEER_EXECUTABLE_PATH=${pkgs.chromium}/bin/chromium
  '';
}
