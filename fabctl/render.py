"""Fills the templates with the facts from model.py and writes rendered/<device>/frr.conf."""
from pathlib import Path

import jinja2

from . import model

TEMPLATES = Path(__file__).parent / "templates"

# StrictUndefined: a typo in a template variable stops the render with an error,
# instead of silently leaving an empty space in a router config.
ENV = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES), undefined=jinja2.StrictUndefined,
                         trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)


def render(intent, out="rendered"):
    """Writes one folder per device and returns the list of files written."""
    written = []
    for name, facts in model.devices(intent).items():
        text = ENV.get_template(f"{facts['role']}.conf.j2").render(**facts)
        path = Path(out) / name / "frr.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        written.append(path)
    return written
