#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLUSTER_NAME="challenge-demo"
DASHBOARD_PORT=3000
API_PORT=8080

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
DIM='\033[2m'
RESET='\033[0m'

step() { echo -e "\n${GREEN}==>${RESET} $1"; }
warn() { echo -e "${YELLOW}⚠  $1${RESET}"; }
fail() { echo -e "${RED}✗  $1${RESET}"; exit 1; }

# --- Prerequisites ---
step "Checking prerequisites..."
for cmd in docker kubectl k3d python3; do
  command -v "$cmd" &>/dev/null || fail "'$cmd' not found. Install it first."
done
docker info &>/dev/null || fail "Docker is not running. Start Docker Desktop first."
echo -e "  docker, kubectl, k3d, python3 ${GREEN}OK${RESET}"

# --- Cluster ---
if k3d cluster list 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  step "Cluster '$CLUSTER_NAME' already exists"
  k3d cluster start "$CLUSTER_NAME" 2>/dev/null || true
else
  step "Creating k3d cluster..."
  k3d cluster create "$CLUSTER_NAME" --agents 2 -p "${API_PORT}:80@loadbalancer"
fi

step "Waiting for cluster to be ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=120s
echo -e "  Waiting for metrics-server..."
for i in $(seq 1 30); do
  kubectl top nodes &>/dev/null && break
  [ "$i" -eq 30 ] && warn "metrics-server not ready yet — HPA may take a moment"
  sleep 2
done

# --- Deploy API ---
step "Building and deploying API..."
docker build -t python-api:latest "$SCRIPT_DIR/api" -q
k3d image import python-api:latest -c "$CLUSTER_NAME" 2>/dev/null
kubectl apply -f "$SCRIPT_DIR/k8s/" > /dev/null
kubectl rollout status deployment/python-api --timeout=120s

# Verify API is reachable via Ingress
for i in $(seq 1 15); do
  if curl -sf "http://localhost:${API_PORT}/health" &>/dev/null; then
    echo -e "  API reachable at ${GREEN}http://localhost:${API_PORT}${RESET}"
    break
  fi
  [ "$i" -eq 15 ] && warn "API not reachable at port ${API_PORT} yet — Traefik may need a moment"
  sleep 1
done

# --- Dashboard venv ---
step "Setting up dashboard..."
VENV_DIR="$SCRIPT_DIR/dashboard/.venv"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install -q -r "$SCRIPT_DIR/dashboard/requirements.txt" 2>&1 | tail -1

# --- Open monitoring terminal ---
step "Opening monitoring terminal..."
osascript <<EOF
tell application "Terminal"
  activate
  do script "cd \"$SCRIPT_DIR\" && echo '=== K8s Monitor ===' && kubectl get pods -l app=python-api -w"
end tell
EOF

# --- Open browser ---
step "Opening dashboard in browser..."
(sleep 2 && open "http://localhost:${DASHBOARD_PORT}") &

# --- Start dashboard ---
step "Starting dashboard on port ${DASHBOARD_PORT}..."
echo -e "  ${DIM}Press Ctrl+C to stop${RESET}\n"
cd "$SCRIPT_DIR/dashboard"
uvicorn app:app --port "$DASHBOARD_PORT"
