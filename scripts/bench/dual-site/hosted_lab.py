"""Resource accounting for the one-job GitHub-hosted replay."""

import os
from pathlib import Path

import run_lab


class VirtualLab(run_lab.Lab):
    thermal_telemetry = "coretemp unavailable on hosted VM"

    def read_temperature(self):
        return None

    async def cool(self):
        # The guest exposes no physical thermal sensor. Its kernel-enforced
        # resource controls were checked before constructing this instance.
        return


def replay_lab(args, root, logs):
    if os.environ.get("REPLAY_HOSTED") != "1":
        return run_lab.Lab(args, root, logs)
    caps = run_lab.resource_caps()
    quota, period = map(int, caps["cpu.max"].split())
    if not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("ALRUST_NETEM_ACTIVE") == "1"
        and os.getuid() != 0
        and 0 < quota <= 2 * period
        and 0 < int(caps["memory.max"]) <= 4 * 1024**3
        and caps["memory.swap.max"] == "0"
    ):
        raise RuntimeError("Hosted replay requires an unprivileged Actions guest with a capped cgroup")
    if any(p.read_text().strip() == "coretemp" for p in Path("/sys/class/hwmon").glob("hwmon*/name")):
        return run_lab.Lab(args, root, logs)
    return VirtualLab(args, root, logs)
