{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  buildInputs = with pkgs; [
    # Kubernetes tools
    kind  # This is what we need!
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
    echo "  kubectl   - Kubernetes CLI"
    echo "  helm      - Helm package manager"
    echo "  k9s       - Terminal UI for Kubernetes"
    echo "  k3s       - Lightweight Kubernetes"
    echo ""

    # Set up kubeconfig if k3s is installed
    if [ -f /etc/rancher/k3s/k3s.yaml ]; then
      export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
      echo "✓ KUBECONFIG set to k3s"
    fi

    # Create aliases
    alias k=kubectl
    alias kns='kubectl config set-context --current --namespace'

    echo "Aliases:"
    echo "  k         - kubectl"
    echo "  kns       - set namespace"
    echo ""
  '';
}
