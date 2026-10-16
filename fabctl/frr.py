"""Compares a running router with the config Git says it must run, the way FRR itself does it.

frr-reload.py ships with FRR. Given a config file, it reads the router's running config and works
out the exact commands that would turn one into the other. With --test it only prints them;
with --reload it sends them, so nothing restarts and untouched parts keep running.

The same code serves the round trip test in the pipeline, the drift check on the
server, and the deploy itself, so all three agree on what "different" means.
"""
import re

RELOAD = "/usr/lib/frr/frr-reload.py"
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

# After a reload, frr-reload saves the result with "write memory" unless the file it was given is
# <confdir>/frr.conf. Here /etc/frr/frr.conf is the boot config from Git, mounted read only on
# purpose, so that save fails and frr-reload reports failure after applying everything. A private
# confdir holding the wanted config as its frr.conf (and the vtysh.conf vtysh reads from there)
# makes the two paths the same: nothing is saved, and the boot config stays exactly what Git says.
CONFDIR = "/tmp/fabctl"
WANTED = f"{CONFDIR}/frr.conf"


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


def stage(execute, config):
    execute("sh", "-c", f"mkdir -p {CONFDIR} && cp /etc/frr/vtysh.conf {CONFDIR}/ && cat > {WANTED}",
            stdin=for_reload(config))


def frr_reload(execute, mode):
    return execute("python3", RELOAD, mode, "--stdout", "--confdir", CONFDIR, WANTED)


def changes(execute, config):
    """What a reload of `config` would change on a router, or None if frr-reload could not run.

    execute(*command, stdin=None) runs a command inside the router and returns a CompletedProcess.
    """
    stage(execute, config)
    result = frr_reload(execute, "--test")
    return parse(result.stdout) if result.returncode == 0 else None


def reload(execute, config):
    """Sends a router only the commands that turn its running config into `config`.
    Returns (True, []) if it worked, or (False, frr-reload's own last lines saying why not)."""
    stage(execute, config)
    result = frr_reload(execute, "--reload")
    if result.returncode == 0:
        return True, []
    lines = [l for l in (result.stdout + result.stderr).splitlines() if l.strip() and " INFO: " not in l]
    return False, lines[-15:]
