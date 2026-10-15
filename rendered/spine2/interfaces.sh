#!/bin/sh
# spine2: Linux side of a spine. Generated from intent/fabric.yml by fabctl render.
# A spine only routes, so it needs no VLANs or tunnels: just forwarding and IPv6 switched on.
set -e

# Forward packets, and enable IPv6: BGP unnumbered talks over IPv6 link local addresses.
sysctl -qw net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1
sysctl -qw net.ipv6.conf.all.disable_ipv6=0 net.ipv6.conf.default.disable_ipv6=0
sysctl -qw net.ipv6.conf.eth11.disable_ipv6=0
ip link set eth11 up
sysctl -qw net.ipv6.conf.eth12.disable_ipv6=0
ip link set eth12 up
sysctl -qw net.ipv6.conf.eth13.disable_ipv6=0
ip link set eth13 up
sysctl -qw net.ipv6.conf.eth14.disable_ipv6=0
ip link set eth14 up
sysctl -qw net.ipv6.conf.eth21.disable_ipv6=0
ip link set eth21 up
sysctl -qw net.ipv6.conf.eth22.disable_ipv6=0
ip link set eth22 up
