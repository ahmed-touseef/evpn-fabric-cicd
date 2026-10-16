"""Command line:
    python3 -m fabctl check     look for mistakes in intent/fabric.yml
    python3 -m fabctl render    check, then write the configs into rendered/
    python3 -m fabctl snapshot  pack rendered/ into a Batfish snapshot in build/batfish/
    python3 -m fabctl agent     on the server: deploy main once GitHub approves it, heal drift
    python3 -m fabctl status    on the server: measure the lab for the status page
    python3 -m fabctl gameday   on the server: break the lab on purpose, measure how it copes
"""
import sys

import yaml

from . import intent


def check():
    """Returns the intent if it is consistent, None after printing the problems."""
    try:
        data = intent.load()
        errors = intent.check(data)
    except (OSError, yaml.YAMLError) as e:
        print(f"Cannot read the intent: {e}")
        return None
    except KeyError as e:
        print(f"Missing field in the intent: {e}")
        return None
    if errors:
        print(f"{len(errors)} problem(s) in intent/fabric.yml:")
        for e in errors:
            print(f"  - {e}")
        return None
    vlans = sum(len(t["vlans"]) for t in data["tenants"].values())
    print(f"intent/fabric.yml is consistent: {len(data['spines']['devices'])} spines, "
          f"{len(data['leaves'])} leaves, {len(data['borders'])} borders, "
          f"{len(data['tenants'])} tenants, {vlans} VLANs, {len(data['hosts'])} servers")
    return data


def render():
    data = check()
    if data is None:
        return 1
    from .render import render as write_configs      # only needs jinja2 when rendering
    for path in write_configs(data):
        print(f"  wrote {path}")
    return 0


def snapshot():
    data = check()
    if data is None:
        return 1
    from .batfish import snapshot as build
    report = build(data)
    reasons = {}
    for device, dropped in report.items():
        for _, why in dropped:
            reasons[why] = reasons.get(why, 0) + 1
    print(f"  wrote build/batfish/: {len(report)} configs and the cabling (layer1_topology.json)")
    print("  lines left out of the analysis, and why:")
    for why, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"    {count:4}  {why}")
    return 0


def agent():
    from .agent import main
    return main(sys.argv[2:])


def status():
    from .status import main
    return main()


def gameday():
    from .gameday import main
    return main()


COMMANDS = {"check": lambda: 0 if check() else 1, "render": render, "snapshot": snapshot, "agent": agent,
            "status": status, "gameday": gameday}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS or (len(sys.argv) > 2 and sys.argv[1] != "agent"):
        print(__doc__.strip())
        sys.exit(2)
    sys.exit(COMMANDS[sys.argv[1]]())
