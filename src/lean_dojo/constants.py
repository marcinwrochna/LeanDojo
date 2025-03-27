import multiprocessing
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__version__ = "3.0.0"


@dataclass
class Config:
    MAX_DEFAULT_NUM_PROCS = 32

    cache_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("CACHE_DIR", "~/.cache/lean_dojo")).expanduser()
    )
    """Cache directory for storing traced repos (see `load_cached_repo_clone`)."""

    remote_cache_url: str = "https://dl.fbaipublicfiles.com/lean-dojo"
    """URL of the remote cache (see `load_cached_repo_clone`)."""

    disable_remote_cache: bool = field(
        default_factory=lambda: _is_true_str(os.environ.get("DISABLE_REMOTE_CACHE", "0"))
    )
    """Whether to disable remote caching (see `load_cached_repo_clone`) and build all repos locally."""

    tmp_dir: Path | Literal[False] = field(
        default_factory=lambda: Path(os.environ["TMP_DIR"]) if "TMP_DIR" in os.environ else False
    )
    """Temporary directory used by LeanDojo for storing intermediate files."""

    num_procs: int = int(os.environ.get("NUM_PROCS", min(multiprocessing.cpu_count(), MAX_DEFAULT_NUM_PROCS)))
    """Number of worker processes or lean threads to use."""

    num_lean_threads: int = 0
    """Number of Lean threads to use during tracing or running a dojo env. Zero means num_procs."""

    num_ray_actors: int = 0
    """Number of Ray actors to use when tracing. Zero means num_procs."""

    lean4_url: str = "https://github.com/leanprover/lean4"
    """The URL of the Lean 4 repo."""

    lean4_nightly_url: str = "https://github.com/leanprover/lean4-nightly"
    """The URL of the nightly Lean 4 build repo."""

    lean4_packages_dir: Path = Path(".lake/packages")  # (since v4.3.0-rc2)
    """The directory where Lean 4 dependencies are stored."""

    load_used_packages_only: bool = field(
        default_factory=lambda: _is_true_str(os.environ.get("LOAD_USED_PACKAGES_ONLY", "0"))
    )
    """Only load depdendency files that are actually used by the target repo."""

    lean4_build_dir: Path = Path(".lake/build")

    tactic_cpu_limit: int = field(default_factory=lambda: int(os.environ.get("TACTIC_CPU_LIMIT", 1)))
    """Number of CPUs for executing tactics when interacting with Lean."""

    tactic_memory_limit: str = field(default_factory=lambda: os.environ.get("TACTIC_MEMORY_LIMIT", "32g"))
    """Maximum memory when interacting with Lean."""

    def __post_init__(self) -> None:
        assert re.fullmatch(r"\d+g", self.tactic_memory_limit)

        if not self.num_lean_threads:
            self.num_lean_threads = self.num_procs

        if not self.num_ray_actors:
            self.num_ray_actors = self.num_procs


def _is_true_str(s: str) -> bool:
    return s.lower() not in ["false", "0", "no", "f", "n"]


global_config = Config()
