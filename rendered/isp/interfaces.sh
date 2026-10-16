#!/bin/sh
# isp: Linux side of the simulated ISP. Generated from intent/fabric.yml by fabctl render.
set -e
# Forward IPv4 and IPv6, like every other router here. IPv6 is set explicitly: a new container's
# default depends on the host, and when it is off FRR writes "no ipv6 forwarding" into the running
# config, so leaving it to chance makes the running config differ from Git on some hosts.
sysctl -qw net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1
ip link set eth21 up
ip link set eth22 up
