#!/usr/bin/env bash
set -euo pipefail

echo "=== Challenge Demo: Cluster Setup ==="

# Check prerequisites
for cmd in docker kubectl k3d; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: '$cmd' is not installed."
    if [ "$cmd" = "k3d" ]; then
      echo "  Install with: brew install k3d"
    elif [ "$cmd" = "kubectl" ]; then
      echo "  Install with: brew install kubectl"
    else
      echo "  Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
    fi
    exit 1
  fi
done

# Check if Docker daemon is running
if ! docker info &>/dev/null; then
  echo "ERROR: Docker daemon is not running. Start Docker Desktop first."
  exit 1
fi

CLUSTER_NAME="challenge-demo"

# Delete existing cluster if present
if k3d cluster list 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  echo "Cluster '$CLUSTER_NAME' already exists. Deleting..."
  k3d cluster delete "$CLUSTER_NAME"
fi

# Create cluster
echo "Creating k3d cluster '$CLUSTER_NAME'..."
k3d cluster create "$CLUSTER_NAME" \
  --agents 2 \
  -p "8080:80@loadbalancer"

# Wait for nodes
echo "Waiting for nodes to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s

# Patch metrics-server for faster refresh (15s → 5s)
echo "Patching metrics-server for 5s resolution..."
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--metric-resolution=5s"}]' \
  2>/dev/null || true

# Wait for metrics-server
echo "Waiting for metrics-server to be available (this may take up to 60s)..."
for i in $(seq 1 30); do
  if kubectl top nodes &>/dev/null; then
    echo "metrics-server is ready!"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo "WARNING: metrics-server not ready after 60s. HPA may not work immediately."
    echo "  Run 'kubectl top nodes' to check later."
  fi
  sleep 2
done

echo ""
echo "=== Cluster Ready ==="
kubectl get nodes
echo ""
echo "Next step: run ./scripts/deploy.sh"
