"""Fills the templates with the facts from model.py and writes the files in rendered/<device>/."""
from pathlib import Path

import jinja2

from . import model

TEMPLATES = Path(__file__).parent / "templates"

# StrictUndefined: a typo in a template variable stops the render with an error,
# instead of silently leaving an empty space in a router config.
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), undefined=jinja2.StrictUndefined,
                         trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)

# Every device gets two files: the FRR config (routing) and a shell script that
# builds its Linux interfaces (the VRFs, VLANs and VXLAN tunnels the kernel forwards with).
FILES = {"frr.conf": "{role}.conf.j2", "interfaces.sh": "{role}.sh.j2"}
HOST_FILES = ("interfaces.sh",)          # servers run no routing software, so no frr.conf


def render(intent, out="rendered"):
    """Writes one folder per device and returns the list of files written."""
    written = []
    for name, facts in model.devices(intent).items():
        for filename, template in FILES.items():
            if facts["role"] == "host" and filename not in HOST_FILES:
                continue
            text = ENV.get_template(template.format(role=facts["role"])).render(**facts)
            path = Path(out) / name / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            written.append(path)
    devices = model.devices(intent)
    topology = ENV.get_template("topology.clab.yml.j2").render(
        name=intent["fabric"]["name"],
        routers=[n for n, d in devices.items() if d["role"] != "host"],
        hosts=[n for n, d in devices.items() if d["role"] == "host"],
        cables=list(model.cables(intent)))
    path = Path(out) / "topology.clab.yml"
    path.write_text(topology)
    written.append(path)
    return written
