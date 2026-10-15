#!/bin/sh
# leaf4: Linux side of a leaf. Generated from intent/fabric.yml by fabctl render.
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

# Tenant storage: its own routing table (VRF), and L3VNI 50002 carrying its routed traffic
exists storage || ip link add storage type vrf table 50002
ip link set storage up
exists br50002 || ip link add br50002 type bridge
ip link set br50002 master storage up
exists vx50002 || ip link add vx50002 mtu 1500 type vxlan id 50002 local 10.255.0.14 dstport 4789 nolearning
ip link set vx50002 master br50002 up

# VLAN 20 (nfs): a bridge with the anycast gateway, joined to the fabric by VNI 10020
exists br20 || ip link add br20 type bridge
ip link set br20 master storage address 00:00:5e:00:01:01 up
ip addr replace 10.20.20.1/24 dev br20
sysctl -qw net.ipv4.conf.br20.arp_accept=1   # learn servers from replies meant for another leaf
exists vx10020 || ip link add vx10020 mtu 1500 type vxlan id 10020 local 10.255.0.14 dstport 4789 nolearning
ip link set vx10020 master br20 up
ip link set vx10020 type bridge_slave neigh_suppress on learning off

# VLAN 21 (backup): a bridge with the anycast gateway, joined to the fabric by VNI 10021
exists br21 || ip link add br21 type bridge
ip link set br21 master storage address 00:00:5e:00:01:01 up
ip addr replace 10.20.21.1/24 dev br21
sysctl -qw net.ipv4.conf.br21.arp_accept=1   # learn servers from replies meant for another leaf
exists vx10021 || ip link add vx10021 mtu 1500 type vxlan id 10021 local 10.255.0.14 dstport 4789 nolearning
ip link set vx10021 master br21 up
ip link set vx10021 type bridge_slave neigh_suppress on learning off

# Server ports: each one joins the bridge of its VLAN. Servers use standard 1500 byte packets;
# the tunnel adds 50 bytes, and fabric cables carry 9500, so a full packet always fits.
ip link set eth3 master br20 mtu 1500 up   # nfs2
ip link set eth4 master br21 mtu 1500 up   # bkp1
