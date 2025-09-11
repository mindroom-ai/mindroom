{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    chromium
    nodejs
  ];

  shellHook = ''
    export PUPPETEER_EXECUTABLE_PATH="${pkgs.chromium}/bin/chromium"
    export PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
    echo "Chromium available at: $PUPPETEER_EXECUTABLE_PATH"
  '';
}
