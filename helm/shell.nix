{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    kubernetes-helm
    kubectl
  ];

  shellHook = ''
    echo "Helm environment loaded"
    echo "Testing MindRoom Helm chart..."
  '';
}
