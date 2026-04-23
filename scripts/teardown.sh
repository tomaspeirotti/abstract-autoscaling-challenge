#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME="challenge-demo"

echo "=== Challenge Demo: Teardown ==="

if k3d cluster list 2>/dev/null | grep -q "$CLUSTER_NAME"; then
  echo "Deleting cluster '$CLUSTER_NAME'..."
  k3d cluster delete "$CLUSTER_NAME"
  echo "Cluster deleted."
else
  echo "Cluster '$CLUSTER_NAME' not found. Nothing to delete."
fi
