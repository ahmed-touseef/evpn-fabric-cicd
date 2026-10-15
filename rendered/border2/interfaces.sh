#!/bin/sh
# border2: Linux side of a border leaf. Generated from intent/fabric.yml by fabctl render.
# No servers here: only the tenant with internet access, and the cable to the ISP inside it.
# Safe to run again: anything that already exists is left as it is.
set -e
exists() { ip link show "$1" >/dev/null 2>&1; }

# Forward packets, and enable IPv6: BGP unnumbered talks over IPv6 link local addresses.
sysctl -qw net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1
sysctl -qw net.ipv6.conf.all.disable_ipv6=0 net.ipv6.conf.default.disable_ipv6=0
sysctl -qw net.ipv6.conf.eth1.disable_ipv6=0
ip link set eth1 up
sysctl -qw net.ipv6.conf.eth2.disable_ipv6=0
ip link set eth2 up

# Tenant prod: its own routing table (VRF), and L3VNI 50001 carrying its routed traffic
exists prod || ip link add prod type vrf table 50001
ip link set prod up
exists br50001 || ip link add br50001 type bridge
ip link set br50001 master prod up
exists vx50001 || ip link add vx50001 mtu 1500 type vxlan id 50001 local 10.255.0.22 dstport 4789 nolearning
ip link set vx50001 master br50001 up

# The cable to the ISP sits inside the prod VRF: nothing else can reach the outside.
ip link set eth3 master prod up
ip addr replace 172.16.255.2/31 dev eth3
