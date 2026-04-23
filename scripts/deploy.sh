#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="challenge-demo"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Challenge Demo: Deploy APIs ==="

# Check cluster exists
if ! k3d cluster list 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  echo "ERROR: Cluster '$CLUSTER_NAME' not found. Run ./scripts/setup.sh first."
  exit 1
fi

# Build Docker images
echo "Building python-api image..."
docker build -t python-api:latest "$PROJECT_DIR/api"

echo "Building rust-api image..."
docker build -t rust-api:latest "$PROJECT_DIR/api-rust"

# Import images into k3d
echo "Importing images into k3d cluster..."
k3d image import python-api:latest rust-api:latest -c "$CLUSTER_NAME"

# Apply core K8s manifests (python deployment + service + HPA + ingress).
# Rust deployment/service/HPA are created on demand by the dashboard when
# dual-stack is enabled from the Cluster config panel.
echo "Applying core Kubernetes manifests..."
kubectl apply -f "$PROJECT_DIR/k8s/deployment.yaml"
kubectl apply -f "$PROJECT_DIR/k8s/service.yaml"
kubectl apply -f "$PROJECT_DIR/k8s/hpa.yaml"
kubectl apply -f "$PROJECT_DIR/k8s/ingress.yaml"

# Wait for pod to be running
echo "Waiting for python-api pod to be ready..."
kubectl rollout status deployment/python-api --timeout=120s

echo ""
echo "=== Deployment Complete ==="
kubectl get pods -l app=python-api
kubectl get hpa
echo ""
echo "APIs reachable at:"
echo "  http://localhost:8080/py/health   (python)"
echo "  http://localhost:8080/rs/health   (rust, only in dual mode)"
