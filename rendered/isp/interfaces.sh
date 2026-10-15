#!/bin/sh
# isp: Linux side of the simulated ISP. Generated from intent/fabric.yml by fabctl render.
set -e
sysctl -qw net.ipv4.ip_forward=1
ip link set eth21 up
ip link set eth22 up
