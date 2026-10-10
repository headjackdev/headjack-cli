#!/usr/bin/env bash
# Restart a single Cassandra node without triggering a full cluster rebalance.
# Usage: restart-node.sh <pod-name>

set -euo pipefail
kubectl delete pod "$1" --grace-period=60
