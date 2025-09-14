{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    # Kubernetes tools
    kind
    kubectl
    kubernetes-helm
    k9s

    # Development tools
    docker
    stern  # multi-pod log tailing

    # Utilities
    jq
    curl
  ];
}
