"""Command line: python3 -m fabctl check"""
import sys

import yaml

from . import intent


def check():
    try:
        data = intent.load()
        errors = intent.check(data)
    except (OSError, yaml.YAMLError) as e:
        print(f"Cannot read the intent: {e}")
        return 1
    except KeyError as e:
        print(f"Missing field in the intent: {e}")
        return 1
    if errors:
        print(f"{len(errors)} problem(s) in intent/fabric.yml:")
        for e in errors:
            print(f"  - {e}")
        return 1
    vlans = sum(len(t["vlans"]) for t in data["tenants"].values())
    print(f"intent/fabric.yml is consistent: {len(data['spines']['devices'])} spines, "
          f"{len(data['leaves'])} leaves, {len(data['borders'])} borders, "
          f"{len(data['tenants'])} tenants, {vlans} VLANs, {len(data['hosts'])} servers")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["check"]:
        print("Use: python3 -m fabctl check")
        sys.exit(2)
    sys.exit(check())
