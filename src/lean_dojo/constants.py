"""Constants controlling LeanDojo's behaviors.
Many of them are configurable via :ref:`environment-variables`.
"""

import os
import re
import sys
import subprocess
import multiprocessing
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple, Literal
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

__version__ = "2.2.0"

logger.remove()
if "VERBOSE" in os.environ or "DEBUG" in os.environ:
    logger.add(sys.stderr, level="DEBUG")
else:
    logger.add(sys.stderr, level="INFO")


def _is_true_str(s: str) -> bool:
    return s.lower() not in ["false", "0", "no", "f", "n"]


@dataclass
class Config:
    MAX_DEFAULT_NUM_PROCS = 32

    cache_dir: Path = Path(
        os.environ.get("CACHE_DIR", "~/.cache/lean_dojo")
    ).expanduser()
    """Cache directory for storing traced repos (see :ref:`caching`)."""

    remote_cache_url: str = "https://dl.fbaipublicfiles.com/lean-dojo"
    """URL of the remote cache (see :ref:`caching`)."""

    disable_remote_cache: bool = _is_true_str(
        os.environ.get("DISABLE_REMOTE_CACHE", "0")
    )
    """Whether to disable remote caching (see :ref:`caching`) and build all repos locally."""

    tmp_dir: Path | Literal[False] = (
        Path(os.environ["TMP_DIR"]) if "TMP_DIR" in os.environ else False
    )
    """Temporary directory used by LeanDojo for storing intermediate files."""

    num_procs: int = int(
        os.environ.get(
            "NUM_PROCS",
            min(multiprocessing.cpu_count(), MAX_DEFAULT_NUM_PROCS),
        )
    )
    """Number of worker processes or lean threads to use."""

    num_lean_threads: int = 0
    """Number of Lean threads to use during tracing or running a dojo env. Zero means num_procs."""

    num_ray_actors: int = 0
    """Number of Ray actors to use when tracing. Zero means num_procs."""

    lean4_url: str = "https://github.com/leanprover/lean4"
    """The URL of the Lean 4 repo."""

    lean4_packages_dir: Path = Path(".lake/packages")  # (since v4.3.0-rc2)
    """The directory where Lean 4 dependencies are stored."""

    load_used_packages_only: bool = _is_true_str(
        os.environ.get("LOAD_USED_PACKAGES_ONLY", "0")
    )
    """Only load depdendency files that are actually used by the target repo."""

    lean4_build_dir: Path = Path(".lake/build")

    tactic_cpu_limit: int = int(os.environ.get("TACTIC_CPU_LIMIT", 1))
    """Number of CPUs for executing tactics when interacting with Lean."""

    tactic_memory_limit: str = os.environ.get("TACTIC_MEMORY_LIMIT", "32g")
    """Maximum memory when interacting with Lean."""

    def __post_init__(self):
        assert re.fullmatch(r"\d+g", self.tactic_memory_limit)

        if self.num_lean_threads == 0:
            self.num_lean_threads = self.num_procs

        if self.num_ray_actors == 0:
            self.num_ray_actors = self.num_procs

        check_git_version((2, 25, 0))


def check_git_version(min_version: Tuple[int, int, int]) -> None:
    """Check the version of Git installed on the system."""
    res = subprocess.run("git --version", shell=True, capture_output=True, check=True)
    output = res.stdout.decode().strip()
    error = res.stderr.decode()
    assert error == "", error
    m = re.search(r"git version (\d+\.\d+\.\d+)", output)
    assert m, f"Could not parse Git version from: {output}"
    # Convert version number string to tuple of integers
    version = tuple(int(_) for _ in m.group(1).split("."))
    version_str = ".".join(str(_) for _ in version)
    min_version_str = ".".join(str(_) for _ in min_version)
    assert version >= min_version, (
        f"Git version {version_str} is too old. Please upgrade to at least {min_version_str}."
    )

global_config = Config()