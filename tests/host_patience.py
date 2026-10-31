"""How long the host tests wait for their stand-in processes: longer on a loaded host.

The tests bound the start of a stand-in engine or worker at 5-8 s, which a
quiet host meets with seconds to spare. Under a heavier load (a phone booting
while the full host suite runs at low priority) such tests fail on those bounds
and pass in isolation. PATIENCE multiplies the tests' own bounds, never the
product's: TOMACCO_TEST_PATIENCE when it is set (a whole number of at least 1,
to pin the factor), otherwise derived once, when this module is first
imported, from the host's 1-minute load: 1 while the load is at most the CPU
count (an unloaded host runs the suite unchanged), otherwise the load per CPU
rounded up, at most LIMIT.
"""
import math
import os
from pathlib import Path

LIMIT = 8
LOADAVG = "/proc/loadavg"


def derive(load, cpus, pinned=None):
    """The factor for a 1-minute load `load` on `cpus` CPUs, or the pinned one."""
    if pinned is not None and pinned.strip():
        try:
            factor = int(pinned.strip())
        except ValueError:
            factor = 0
        if factor < 1:
            raise ValueError(f"TOMACCO_TEST_PATIENCE must be a whole number of at least 1, not {pinned!r}")
        return factor
    cpus = max(1, int(cpus or 1))
    if not load > cpus:
        return 1
    return min(LIMIT, math.ceil(load / cpus))


def host_load(path=LOADAVG):
    """The host's 1-minute load average; 0 where it cannot be read."""
    try:
        return float(Path(path).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def patience(environment=os.environ, path=LOADAVG, cpus=None):
    """The factor for this host: pinned by the environment, else from its load."""
    return derive(host_load(path), os.cpu_count() if cpus is None else cpus,
                  environment.get("TOMACCO_TEST_PATIENCE"))


PATIENCE = patience()
