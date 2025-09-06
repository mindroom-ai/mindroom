{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    # Kubernetes tools
    kind  # Kubernetes in Docker for local testing
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

  shellHook = ''
    echo "🚀 MindRoom K8s Development Shell"
    echo "================================"
    echo ""
    echo "Available commands:"
    echo "  kind      - Local Kubernetes clusters using Docker"
    echo "  kubectl   - Kubernetes CLI"
    echo "  helm      - Helm package manager"
    echo "  k9s       - Terminal UI for Kubernetes"
    echo "  stern     - Multi-pod log tailing"
    echo ""

    # Create aliases
    alias k=kubectl
    alias kns='kubectl config set-context --current --namespace'
    alias h=helm

    echo "Aliases:"
    echo "  k         - kubectl"
    echo "  kns       - set namespace"
    echo "  h         - helm"
    echo ""

    echo "Quick start:"
    echo "  1. kind create cluster --name mindroom"
    echo "  2. cd mindroom && ./setup.sh demo demo.local"
    echo ""
  '';
}
