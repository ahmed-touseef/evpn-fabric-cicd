"""Compares a running router with the config Git says it must run, the way FRR itself does it.

frr-reload.py ships with FRR. Given a config file, it reads the router's running config and works
out the exact commands that would turn one into the other. With --test it only prints them;
with --reload it sends them, so nothing restarts and untouched parts keep running.

The same three functions serve the round trip test in the pipeline, the drift check on the
server, and the deploy itself, so all three agree on what "different" means.
"""
import re

RELOAD = "/usr/lib/frr/frr-reload.py"
WANTED = "/tmp/frr.wanted"
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def for_reload(config):
    """The config as frr-reload must see it: without the version line, which every FRR build stamps
    with its own suffix (10.2.1 becomes 10.2.1_git) and frr-reload cannot change anyway."""
    return "".join(line for line in config.splitlines(keepends=True) if not line.startswith("frr version"))


def parse(output):
    """(lines frr-reload would delete, lines it would add), each with their surrounding context
    such as "router bgp 65101". Both empty means the router already runs exactly this config."""
    delete, add, section = [], [], None
    for line in output.splitlines():
        if line in ("Lines To Delete", "Lines To Add"):
            section = delete if line == "Lines To Delete" else add
        elif section is not None and line.strip() and set(line) != {"="} and not LOG_LINE.match(line):
            section.append(line)
    return delete, add


def changes(execute, config):
    """What a reload of `config` would change on a router, or None if frr-reload could not run.

    execute(*command, stdin=None) runs a command inside the router and returns a CompletedProcess.
    """
    execute("sh", "-c", f"cat > {WANTED}", stdin=for_reload(config))
    result = execute("python3", RELOAD, "--test", "--stdout", WANTED)
    return parse(result.stdout) if result.returncode == 0 else None


def reload(execute, config):
    """Sends a router only the commands that turn its running config into `config`. True if it worked."""
    execute("sh", "-c", f"cat > {WANTED}", stdin=for_reload(config))
    return execute("python3", RELOAD, "--reload", "--stdout", WANTED).returncode == 0
