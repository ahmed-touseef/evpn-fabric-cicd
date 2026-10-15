#!/bin/sh
# leaf3: Linux side of a leaf. Generated from intent/fabric.yml by fabctl render.
# FRR decides where packets go; the Linux kernel moves them. This script builds what the
# kernel needs for that: a VRF per tenant, a bridge per VLAN, a VXLAN tunnel per VNI.
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
exists vx50001 || ip link add vx50001 mtu 1500 type vxlan id 50001 local 10.255.0.13 dstport 4789 nolearning
ip link set vx50001 master br50001 up

# VLAN 10 (web): a bridge with the anycast gateway, joined to the fabric by VNI 10010
exists br10 || ip link add br10 type bridge
ip link set br10 master prod address 00:00:5e:00:01:01 up
ip addr replace 10.10.10.1/24 dev br10
sysctl -qw net.ipv4.conf.br10.arp_accept=1   # learn servers from replies meant for another leaf
exists vx10010 || ip link add vx10010 mtu 1500 type vxlan id 10010 local 10.255.0.13 dstport 4789 nolearning
ip link set vx10010 master br10 up
ip link set vx10010 type bridge_slave neigh_suppress on learning off

# VLAN 11 (app): a bridge with the anycast gateway, joined to the fabric by VNI 10011
exists br11 || ip link add br11 type bridge
ip link set br11 master prod address 00:00:5e:00:01:01 up
ip addr replace 10.10.11.1/24 dev br11
sysctl -qw net.ipv4.conf.br11.arp_accept=1   # learn servers from replies meant for another leaf
exists vx10011 || ip link add vx10011 mtu 1500 type vxlan id 10011 local 10.255.0.13 dstport 4789 nolearning
ip link set vx10011 master br11 up
ip link set vx10011 type bridge_slave neigh_suppress on learning off

# Server ports: each one joins the bridge of its VLAN. Servers use standard 1500 byte packets;
# the tunnel adds 50 bytes, and fabric cables carry 9500, so a full packet always fits.
ip link set eth3 master br11 mtu 1500 up   # app1
ip link set eth4 master br10 mtu 1500 up   # web3
