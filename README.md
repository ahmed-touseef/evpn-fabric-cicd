# EVPN fabric CI/CD

An EVPN VXLAN data center fabric that nobody configures by hand. The network is described once,
in `intent/fabric.yml`. Every change is a pull request that has to pass static analysis, a formal
model of the network in Batfish and 58 tests on a real booted copy of the fabric before it can
merge. A deploy agent on the server then pulls the approved commit and rolls it out router by
router, canary first, with automatic rollback.

Live status of the running fabric: **https://cicd.touseefahmed.com**

```
                    spine1                           spine2                   AS 65100
          (every leaf and border has one cable to each spine)

  leaf1       leaf2         leaf3         leaf4          border1   border2    AS 65101 to 65104, 65201, 65202
    |         |    |        |    |        |    |               \     /
  web1      web2  nfs1    app1  web3    nfs2  bkp1                isp         AS 64496
                                                                   |
  prod: web1 web2 web3 app1                                   198.51.100.1    the internet
  storage: nfs1 nfs2 bkp1
```

## The network

| Layer | Design |
|---|---|
| Underlay | eBGP unnumbered (RFC 5549 next hops, the RFC 7938 data center design): spines share AS 65100, every leaf and border has its own AS, so plain BGP loop prevention keeps paths valley free. ECMP over every spine. |
| Failure detection | BFD on every fabric link, 200 ms x 3. A dead neighbour is noticed within 600 ms, where the BGP hold timer alone would take 9 s. |
| Overlay | BGP EVPN with VXLAN. Symmetric IRB: one L3VNI per tenant (prod 50001, storage 50002), so leaves route between subnets without carrying every VLAN. |
| Gateway | Distributed anycast gateway: the `.1` of every subnet, with the same MAC (`00:00:5e:00:01:01`) on every leaf. ARP suppression on the VXLAN ports. |
| Route targets | Set explicitly to `65000:<VNI>`. Automatic route targets embed the leaf's own AS, and with a different AS on every leaf they would never match. |
| Tenants | `prod` (web and app subnets, internet access) and `storage` (NFS and backup, no internet). Each tenant is a VRF on every switch: they cannot reach each other at all. |
| Internet edge | Two borders peer with the ISP inside the prod VRF. They announce only the tenant summary `10.10.0.0/16` (anchored to a blackhole route, so it never flaps) and accept only a default route. Policies: `TO-ISP`, `FROM-ISP`, `DEFAULT-ONLY`, `LOOPBACKS-ONLY`. |

Everything runs on one server: FRR 10.2.1 routers and Alpine servers in containerlab 0.79.0.
The Linux side of each switch (VRFs, bridges, VXLAN devices, addresses) is a rendered shell script;
FRR does the routing.

## How a change gets in

```
intent/fabric.yml ──> fabctl render ──> rendered/ (committed) ──> pull request
                                                                      │
        ┌─────────────────────────────────────────────────────────────┘
        ▼
 1. Intent and configs ──> 2. Batfish ──> 3. Live lab ──> merge to main ──> deploy agent on the server
```

The three jobs of `.github/workflows/pipeline.yml` are required checks on `main`, enforced by a
repository ruleset. Each job only runs if the previous one passed.

**1. Intent and configs.** `fabctl check` validates the intent: unique names, ids, AS numbers and
VNIs, no overlapping subnets, servers on free ports with addresses inside their VLAN, and every
subnet of a tenant inside the summary it announces. The unit tests prove the deploy agent's
decisions. The configs in `rendered/` must be byte for byte what the intent produces, so a pull
request always shows the exact router lines it changes. Every config is parsed by `vtysh -C` in
the real FRR image, and every shell script passes shellcheck.

**2. Batfish.** The rendered configs and the cabling become a Batfish snapshot. Batfish models the
whole network from the configs alone and proves: every BGP session comes up with the right peer;
no route map or prefix list is referenced without being defined (FRR accepts such a typo silently
and filters nothing); and, by symbolic analysis of every policy, that no route the intent forbids
can ever pass. The ISP must get exactly the summaries, and nothing else.

**3. Live lab.** The fabric boots in containerlab on GitHub's machine, and 58 tests check it as it
runs. Every expectation is computed from the intent, never read from the network:

* every router runs exactly its rendered config: `frr-reload --test` must find nothing to change
* a reload of that config works and changes nothing
* every BGP session is up to the neighbour the cabling says, and BFD runs on every fabric link
  with the intent's timers
* ECMP through every spine, every VNI knows exactly the remote VTEPs that carry it
* each leaf holds only its own tenant's routes plus the default route through both borders
* every server pair: allowed pairs answer from a cold start, forbidden pairs never answer, and
  1500 byte packets with don't fragment cross the tunnels
* failure tests: a frozen spine (`docker pause`, the failure only BFD can see), a spine losing every
  cable, a border going down. Continuous 50 ms pings measure the outage, which must stay within the
  budget the intent implies: BFD interval x multiplier, plus 0.7 s to reroute
* an operations rehearsal: a config change goes out with frr-reload and back without dropping a
  single BGP session
* the status page's collector reports the healthy fabric as healthy

If anything fails, `ci/live_explore.py` dumps what the fabric looked like into the job log.

## How it reaches the server

`fabctl/agent.py`, run by a systemd timer every minute in a detached checkout at `/srv/fabric`.
The server pulls; GitHub holds no key to it.

1. Fetch `main`. A new commit is taken only if it descends from the running one and GitHub's check
   runs API reports all three required checks green for exactly that commit.
2. Plan from the files that changed:
   * nothing the network runs (docs, tests, the pipeline): record it, done
   * router configs only: `frr-reload` on one router first, the canary. It must settle and stay
     healthy for 20 s, then the others follow one by one, each checked before the next
   * anything else (Linux side, cabling, a new device): rebuild the lab from the new commit
3. Verify with the pipeline's own live tests. Any failure rolls back to the previous commit.
4. With nothing new to deploy, compare every router with Git and undo anything typed by hand.
   A file of the checkout edited on the server is restored from Git.

A lab found incomplete, after a reboot for example, is rebuilt from the running commit. A commit
that changes the agent itself is deployed by the new code: the old process hands over with
`os.execv`, so a fix to the deploy path can always deploy itself. Every decision goes to a history
file that the status page shows.

## Game day

Passing on GitHub's machine proves the design. It says nothing about this server tonight. So every
night at 03:30 UTC `fabric-gameday.timer` runs the failure tests against the production lab, holding
the agent's lock so no deploy starts meanwhile. It keeps the measured numbers (how fast each switch
noticed the frozen spine, the longest gap in pings) for the status page. Afterwards it unfreezes and
reconnects everything once more and, if the fabric is still not whole, rebuilds it, even when
systemd stops it halfway.

## The status page

`fabctl/status.py` runs every 30 s and writes one JSON file: per router sessions, BFD, ECMP and
EVPN state, every cable, a full ping matrix between servers, what the ISP receives, the agent's
history and the last game day. It only reads, so a bug in it can make the page wrong but never the
network. `deploy/web/` draws it with plain JavaScript and SVG. Nginx serves it with a strict
Content Security Policy (no inline code, Trusted Types enforced), rate limits and read only methods.

## Bugs the pipeline caught

**Quiet servers could not be reached across subnets.** `bkp1` could not reach `nfs1` until `nfs1`
had sent a packet itself. The gateway has the same address and MAC on every leaf. leaf4 carries
VLAN 20 too, so it ARPs for `nfs1` directly. The request floods through VXLAN, and `nfs1` replies to
the gateway MAC, which is local on its own leaf2. leaf2 never asked, so Linux dropped the reply and
nobody learned `nfs1`. The ping matrix from a cold start found it; `arp_accept` on the leaf bridges
fixed it: leaf2 now learns the server from that reply and announces it to the fabric in EVPN.
Confirmed on the lab: 0 of 5 pings before, 5 of 5 after.

**Our own config would have switched BFD off.** FRR stores some lines in its own form: it writes
`neighbor FABRIC bfd` as well as the profile line, and leaves defaults out. A reload of the
rendered config would have sent `no neighbor FABRIC bfd` to every switch. The templates now write
what FRR writes, and the round trip test (reload the rendered config, nothing may change) keeps
it that way, which is also what makes the agent's drift check trustworthy.

**frr-reload reported failure after applying everything.** The first config deploy was rolled back
by its canary. The agent was right to do it: frr-reload had exited with an error. The cause, found in
FRR's source and reproduced with a local FRR: after a reload, frr-reload saves the config with
`write memory` unless the file it was given is `<confdir>/frr.conf`. Here `/etc/frr/frr.conf` is the
config from Git, mounted read only on purpose, so the save failed. The wanted config is now staged
as `frr.conf` in a private confdir, so nothing is saved and the boot config stays what Git says.
That fix changed the agent's own deploy code, which is why the agent now hands over to new code.

## Layout

| Path | What it is |
|---|---|
| `intent/fabric.yml` | The only file a change normally touches |
| `fabctl/` | `intent` and `model` (checks and per device facts), `render` and `templates/`, `batfish` (snapshot), `lab` (probes shared by tests, agent and collector), `frr` (frr-reload), `agent`, `gameday`, `status` |
| `rendered/` | Generated configs, committed |
| `lab/` | `deploy.sh` boots the fabric from `rendered/`; the server image |
| `tests/` | `unit/`, `batfish/`, `live/` |
| `deploy/` | systemd units, the status page, Nginx settings, `publish.sh` |

## Running it

On a Linux machine with Docker, containerlab 0.79.0 and the `vrf` and `vxlan` kernel modules:

```
python3 -m fabctl check               # is the intent consistent
python3 -m fabctl render              # write rendered/
./lab/deploy.sh                       # boot the fabric
python3 -m pytest tests/live -v       # test it, failures included
```

As a deployment target, with the agent and the status page:

```
sudo git clone https://github.com/ahmed-touseef/evpn-fabric-cicd /srv/fabric
cd /srv/fabric && sudo git checkout --detach && sudo ./lab/deploy.sh
sudo cp deploy/fabric-agent.* /etc/systemd/system/ && sudo systemctl enable --now fabric-agent.timer
sudo bash deploy/publish.sh
```

Built by Touseef Ahmed, [www.touseefahmed.com](https://www.touseefahmed.com).
