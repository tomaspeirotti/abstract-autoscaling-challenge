#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="challenge-demo"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Challenge Demo: Deploy API ==="

# Check cluster exists
if ! k3d cluster list 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  echo "ERROR: Cluster '$CLUSTER_NAME' not found. Run ./scripts/setup.sh first."
  exit 1
fi

# Build Docker image
echo "Building API image..."
docker build -t python-api:latest "$PROJECT_DIR/api"

# Import image into k3d
echo "Importing image into k3d cluster..."
k3d image import python-api:latest -c "$CLUSTER_NAME"

# Apply K8s manifests
echo "Applying Kubernetes manifests..."
kubectl apply -f "$PROJECT_DIR/k8s/"

# Wait for pod to be running
echo "Waiting for API pod to be ready..."
kubectl rollout status deployment/python-api --timeout=120s

echo ""
echo "=== Deployment Complete ==="
kubectl get pods -l app=python-api
kubectl get hpa
echo ""
echo "To access the API, run:"
echo "  kubectl port-forward svc/python-api 9090:80"
echo "  curl http://localhost:9090/health"
