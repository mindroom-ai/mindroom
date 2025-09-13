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

    # uv for Python package management
    uv

    # Infra tooling
    terraform
    kubectl
    kubernetes-helm
    kubeconform
  ];

  shellHook = ''
    echo "MindRoom Widget Development Shell"
    echo "Tools available: uv, pnpm, nodejs, python3, chromium"
    echo "Chromium available for screenshots"
    export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
    export PUPPETEER_EXECUTABLE_PATH=${pkgs.chromium}/bin/chromium

    echo "Tip: run backend tests with:"
    echo "  (cd saas-platform/platform-backend && PYTHONPATH=src uv run pytest -q)"
    echo "Render Helm templates with:"
    echo "  helm template platform ./saas-platform/k8s/platform -f saas-platform/k8s/platform/values.yaml | kubeconform -ignore-missing-schemas"
  '';
}
