#!/usr/bin/env bash
# Boots the whole fabric on this server from rendered/. Safe to run again: it starts clean.
#   python3 -m fabctl render && ./lab/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."
LAB=$(awk '/^name:/ {print $2}' rendered/topology.clab.yml)

echo "== 1. Server image =="
sudo docker image inspect fabric-host:1 >/dev/null 2>&1 || sudo docker build -q -t fabric-host:1 lab/host >/dev/null
echo "  fabric-host:1 ready"

echo "== 2. Containers and cables =="
sudo containerlab deploy -t rendered/topology.clab.yml --reconfigure >/dev/null
echo "  $(sudo docker ps -q --filter "label=containerlab=$LAB" | wc -l) containers running"

echo "== 3. Linux side of every device (VRFs, VLANs, VXLAN tunnels, addresses) =="
for dir in rendered/*/; do
  node=$(basename "$dir")
  sudo docker exec "clab-$LAB-$node" sh /etc/fabric/interfaces.sh
  echo "  $node"
done
echo "Done. Give BGP about 10 seconds, then check it."
