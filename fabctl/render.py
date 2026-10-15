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


def render(intent, out="rendered"):
    """Writes one folder per device and returns the list of files written."""
    written = []
    for name, facts in model.devices(intent).items():
        for filename, template in FILES.items():
            text = ENV.get_template(template.format(role=facts["role"])).render(**facts)
            path = Path(out) / name / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            written.append(path)
    return written
