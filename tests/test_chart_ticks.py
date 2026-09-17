"""Table-driven coverage for static/chart.js's tick overlap-rejection logic.

The SoC chart on the car page (static/car.js's renderSoc) used to thin its
x-axis labels by keeping "every Nth row" -- fine when positions are evenly
spaced by construction (index/bucket-positioned bars), but the SoC chart
positions ticks by real elapsed time, and real sample gaps range from ~60s to
over 6000s. A fixed stride over uneven positions still lets labels bunch up
and overlap wherever samples cluster.

static/chart.js now exports pickNonOverlappingTicks / estimateLabelWidth: a
walk over candidate tick positions (in x order) that drops any candidate
whose label would collide with the previously *kept* one, always keeping the
first and last. That is a pure function over plain data (positions + label
widths), so it is tested directly here via Node rather than reimplemented in
Python -- this runs the exact module the browser loads.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "tests" / "chart_ticks_check.mjs"


def test_pick_non_overlapping_ticks_never_lets_two_labels_collide():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available in this environment")

    result = subprocess.run(
        [node, str(CHECK_SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        "chart tick overlap-rejection check failed "
        f"(see static/chart.js pickNonOverlappingTicks):\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
