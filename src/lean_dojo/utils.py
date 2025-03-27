import hashlib
import os
import re
import subprocess
import tempfile
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import ray
from loguru import logger
from ray.util.actor_pool import ActorPool

from .constants import global_config


@contextmanager
def working_directory(path: str | Path | None = None) -> Iterator[Path]:
    """
    Context manager setting the current working directory (CWD) to `path`.

    The original CWD is restored after the context manager exits.

    Args:
        path: The desired CWD. Defaults to None, which means a temporary directory.

    Yields: a `Path` object representing the CWD.
    """
    origin = Path.cwd()
    if path is None:
        tmp_dir = tempfile.TemporaryDirectory(dir=global_config.tmp_dir or None)
        path = tmp_dir.__enter__()
        is_temporary = True
    else:
        is_temporary = False

    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True)
    os.chdir(path)

    try:
        yield path
    finally:
        os.chdir(origin)
        if is_temporary:
            tmp_dir.__exit__(None, None, None)


@contextmanager
def ray_actor_pool(actor_cls: type, *args: Any, **kwargs: Any) -> Iterator[ActorPool]:
    """Create a pool of Ray Actors of class ``actor_cls``.

    Args:
        actor_cls (type): A Ray Actor class (annotated by ``@ray.remote``).
        *args: Position arguments passed to ``actor_cls``.
        **kwargs: Keyword arguments passed to ``actor_cls``.

    Yields: A :class:`ray.util.actor_pool.ActorPool` object.
    """
    assert not ray.is_initialized()
    ray.init()
    pool = ActorPool([actor_cls.remote(*args, **kwargs) for _ in range(global_config.n_ray_actors)])  # type: ignore
    try:
        yield pool
    finally:
        ray.shutdown()


@contextmanager
def report_critical_failure(msg: str) -> Iterator[None]:
    """Context manager logging ``msg`` in case of any exception.

    Args:
        msg (str): The message to log in case of exceptions.

    Raises:
        ex: Any exception that may be raised within the context manager.
    """
    try:
        yield
    except Exception as ex:
        logger.error(msg)
        raise ex


def execute(cmd: str | list[str], capture_output: bool = False) -> tuple[str, str] | None:
    """Execute the shell command ``cmd`` and optionally return its output.

    Args:
        cmd: The shell command to execute.
        capture_output: Whether to capture and return the output. Defaults to False.

    Returns: (stdout, stderr) if capture_output is True else None.
    """
    logger.debug(cmd)
    try:
        res = subprocess.run(cmd, shell=True, capture_output=capture_output, check=True, encoding="utf-8")
    except subprocess.CalledProcessError as ex:
        if capture_output:
            logger.info(ex.stdout)
            logger.error(ex.stderr)
        raise ex
    if not capture_output:
        return None
    return res.stdout, res.stderr


def compute_md5(path: Path) -> str:
    """Return the MD5 hash of the file ``path``."""
    # The file could be large
    # See: https://stackoverflow.com/questions/48122798/oserror-errno-22-invalid-argument-when-reading-a-huge-file
    hasher = hashlib.md5()
    with path.open("rb") as inp:
        while True:
            block = inp.read(64 * (1 << 20))
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


_CAMEL_CASE_REGEX = re.compile(r"(_|-)+")


def camel_case(s: str) -> str:
    """Convert the string ``s`` to camel case."""
    return _CAMEL_CASE_REGEX.sub(" ", s).title().replace(" ", "")


def is_optional_type(tp: type) -> bool:
    """Test if ``tp`` is Optional[X]."""
    if typing.get_origin(tp) != typing.Union:
        return False
    args = typing.get_args(tp)
    return args[-1] is type(None)


def remove_optional_type(tp: type) -> type:
    """Given Optional[X], return X."""
    assert typing.get_origin(tp) == typing.Union
    args: tuple[type, ...] = typing.get_args(tp)
    if args[-1] is type(None):
        if len(args) == 2:
            return args[0]
        else:
            return typing.Union[args[:-1]]  # type: ignore  # noqa: UP007
    else:
        raise ValueError(f"{tp} is not Optional")


def parse_int_list(s: str) -> list[int]:
    assert s.startswith("[") and s.endswith("]")
    return [int(_) for _ in s[1:-1].split(",") if _ != ""]


def parse_str_list(s: str) -> list[str]:
    assert s.startswith("[") and s.endswith("]")
    return [_.strip()[1:-1] for _ in s[1:-1].split(",") if _ != ""]


def _from_lean_path(root_dir: Path, path: Path, _repo: Any, ext: str) -> Path:
    assert path.suffix == ".lean"
    if path.is_absolute():
        path = path.relative_to(root_dir)

    LEAN4_PACKAGES_DIR = global_config.lean4_packages_dir
    LEAN4_BUILD_DIR = global_config.lean4_build_dir

    assert root_dir.name != "lean4"
    if path.is_relative_to(LEAN4_PACKAGES_DIR / "lean4/src/lean/lake"):
        # E.g., "lake-packages/lean4/src/lean/lake/Lake/CLI/Error.lean"
        p = path.relative_to(LEAN4_PACKAGES_DIR / "lean4/src/lean/lake")
        return LEAN4_PACKAGES_DIR / "lean4/lib/lean" / p.with_suffix(ext)
    elif path.is_relative_to(LEAN4_PACKAGES_DIR / "lean4/src"):
        # E.g., "lake-packages/lean4/src/lean/Init.lean"
        p = path.relative_to(LEAN4_PACKAGES_DIR / "lean4/src").with_suffix(ext)
        return LEAN4_PACKAGES_DIR / "lean4/lib" / p
    elif path.is_relative_to(LEAN4_PACKAGES_DIR):
        # E.g., "lake-packages/std/Std.lean"
        p = path.relative_to(LEAN4_PACKAGES_DIR).with_suffix(ext)
        repo_name = p.parts[0]
        return LEAN4_PACKAGES_DIR / repo_name / LEAN4_BUILD_DIR / "ir" / p.relative_to(repo_name)
    else:
        # E.g., "Mathlib/LinearAlgebra/Basics.lean"
        return LEAN4_BUILD_DIR / "ir" / path.with_suffix(ext)


def to_xml_path(root_dir: Path, path: Path, repo: Any) -> Path:
    return _from_lean_path(root_dir, path, repo, ext=".trace.xml")


def to_dep_path(root_dir: Path, path: Path, repo: Any) -> Path:
    return _from_lean_path(root_dir, path, repo, ext=".dep_paths")


def to_json_path(root_dir: Path, path: Path, repo: Any) -> Path:
    return _from_lean_path(root_dir, path, repo, ext=".ast.json")


def to_lean_path(root_dir: Path, path: Path) -> Path:
    if path.is_absolute():
        path = path.relative_to(root_dir)

    if path.suffix in (".xml", ".json"):
        path = path.with_suffix("").with_suffix(".lean")
    else:
        assert path.suffix == ".dep_paths"
        path = path.with_suffix(".lean")

    LEAN4_PACKAGES_DIR = global_config.lean4_packages_dir
    LEAN4_BUILD_DIR = global_config.lean4_build_dir

    assert root_dir.name != "lean4"
    if path == LEAN4_PACKAGES_DIR / "lean4/lib/lean/Lake.lean":
        return LEAN4_PACKAGES_DIR / "lean4/src/lean/lake/Lake.lean"
    elif path == LEAN4_PACKAGES_DIR / "lean4/lib/lean/LakeMain.lean":
        return LEAN4_PACKAGES_DIR / "lean4/src/lean/lake/LakeMain.lean"
    elif path.is_relative_to(LEAN4_PACKAGES_DIR / "lean4/lib/lean/Lake"):
        # E.g., "lake-packages/lean4/lib/lean/Lake/Util/List.lean"
        p = path.relative_to(LEAN4_PACKAGES_DIR / "lean4/lib/lean/Lake")
        return LEAN4_PACKAGES_DIR / "lean4/src/lean/lake/Lake" / p
    elif path.is_relative_to(LEAN4_PACKAGES_DIR / "lean4/lib"):
        # E.g., "lake-packages/lean4/lib/lean/Init.lean"
        p = path.relative_to(LEAN4_PACKAGES_DIR / "lean4/lib")
        return LEAN4_PACKAGES_DIR / "lean4/src" / p
    elif path.is_relative_to(LEAN4_PACKAGES_DIR):
        # E.g., "lake-packages/std/build/ir/Std.lean"
        p = path.relative_to(LEAN4_PACKAGES_DIR)
        repo_name = p.parts[0]
        return LEAN4_PACKAGES_DIR / repo_name / p.relative_to(Path(repo_name) / LEAN4_BUILD_DIR / "ir")
    else:
        # E.g., ".lake/build/ir/Mathlib/LinearAlgebra/Basics.lean" or "build/ir/Mathlib/LinearAlgebra/Basics.lean"
        assert path.is_relative_to(LEAN4_BUILD_DIR / "ir"), path
        return path.relative_to(LEAN4_BUILD_DIR / "ir")
