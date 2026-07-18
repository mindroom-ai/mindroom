{ pkgs ? import <nixpkgs> {} }:

let
  linuxPackages = pkgs.lib.optionals pkgs.stdenv.isLinux (with pkgs; [
    # Chromium for Puppeteer
    chromium

    # Required Chromium libraries
    glib
    nss
    nspr
    atk
    cups
    dbus
    libdrm
    libxcomposite
    libxdamage
    libxext
    libxfixes
    libxrandr
    libxcb
    expat
    alsa-lib
    pango
    cairo
    at-spi2-atk
    at-spi2-core

    # Required for pip-installed native extensions (numpy, chromadb, etc.)
    stdenv.cc.cc.lib
    zlib
  ]);
in
pkgs.mkShell {
  buildInputs = (with pkgs; [
    # Node.js and bun for running the widget
    nodejs_24
    bun

    # uv for Python package management
    uv
  ]) ++ linuxPackages;

  shellHook = ''
    echo "MindRoom Development Shell"
    echo "Tools available: uv, bun, nodejs, python3"
    ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''
      export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
      export PUPPETEER_EXECUTABLE_PATH=${pkgs.chromium}/bin/chromium
      export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ]}:$LD_LIBRARY_PATH"
      echo "Linux Chromium available: $PUPPETEER_EXECUTABLE_PATH"
    ''}

    echo ""
    echo "Run MindRoom locally:"
    echo "  ./run-nix.sh           # Start backend + frontend"
    echo ""
    echo "Run tests:"
    echo "  uv run pytest -q       # Backend tests"
    echo "  cd frontend && bun test   # Frontend tests"
  '';
}
