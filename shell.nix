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

    # Node.js and bun for running the widget
    nodejs_20
    bun

    # uv for Python package management
    uv

    # Required for pip-installed native extensions (numpy, chromadb, etc.)
    stdenv.cc.cc.lib
    zlib
  ];

  shellHook = ''
    echo "MindRoom Development Shell"
    echo "Tools available: uv, bun, nodejs, python3, chromium"
    export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
    export PUPPETEER_EXECUTABLE_PATH=${pkgs.chromium}/bin/chromium
    export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ]}:$LD_LIBRARY_PATH"

    echo ""
    echo "Run MindRoom locally:"
    echo "  ./run-nix.sh           # Start backend + frontend"
    echo ""
    echo "Run tests:"
    echo "  uv run pytest -q       # Backend tests"
    echo "  cd frontend && bun test   # Frontend tests"
  '';
}
