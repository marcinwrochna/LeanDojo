import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Self

from loguru import logger

from ..constants import global_config
from ..git_repo import GitURL, GitRev, load_cached_repo_clone, get_latest_commit, get_friendly_repo_name


class LeanGitRepo:
    """Git repo of a Lean project."""

    def __init__(
        self, url: GitURL, rev: GitRev, cache_dir: Path | None = None, sparse: bool = False, assert_cached: bool = False
    ):
        """Loads repo from cache or clones and stores in cache; see `load_cached_repo_clone()` for details."""
        self.url = url
        self.rev = rev
        self._sparse = sparse
        self._cache_dir = cache_dir or global_config.cache_dir
        self._assert_cached = assert_cached

        self.repo_dir = load_cached_repo_clone(
            url, rev, cache_dir=self._cache_dir, sparse=sparse, assert_cached=assert_cached
        )
        if not is_supported_version(self.lean_toolchain):
            logger.warning(f"{self} relies on an unsupported Lean version: {self.lean_toolchain}")

    @property
    def commit(self) -> str:
        """Git commit hash of the repo (this is cached until python exits)."""
        return get_latest_commit(self.url, self.rev)

    @property
    def friendly_name(self) -> str:
        """See `get_friendly_repo_name`."""
        return get_friendly_repo_name(self.url)

    @property
    def lean_toolchain(self) -> str:
        return (self.repo_dir / "lean-toolchain").read_text().strip()

    @property
    def sparse(self) -> bool:
        """If True, only files in the toplevel dir are required to be checked-out. Set to False to fetch all."""
        return self._sparse

    @sparse.setter
    def sparse(self, value: bool) -> None:
        if value != self._sparse and not value:
            self.repo_dir = load_cached_repo_clone(
                self.url, self.rev, cache_dir=self._cache_dir, sparse=value, assert_cached=self._assert_cached
            )
        self._sparse = value

    def __str__(self) -> str:
        return f"LeanGitRepo({self.url}@{self.rev}, {self.repo_dir}, sparse={self.sparse})"

    def get_dependencies(self) -> dict[str, tuple[GitURL, GitRev]]:
        """Return the dependencies required by the repo (dict from name to (url, rev) pairs)."""

        deps = {"lean4": get_lean4_dep_from_toolchain(self.lean_toolchain)}

        if (self.repo_dir / "lake-manifest.json").exists():
            manifest = json.loads((self.repo_dir / "lake-manifest.json").read_bytes())
            for pkg in manifest["packages"]:
                deps[pkg["name"]] = (pkg["url"], pkg["rev"])
        else:
            for name, (dep_url, dep_commit) in self._parse_lakefile_dependencies().items():
                if name not in deps:
                    deps[name] = (dep_url, dep_commit)
                    subrepo = LeanGitRepo(
                        dep_url, dep_commit, cache_dir=self._cache_dir, sparse=True, assert_cached=self._assert_cached
                    )
                    deps = subrepo.get_dependencies() | deps

        return deps

    def _parse_lakefile_dependencies(self) -> dict[str, tuple[GitURL, GitRev]]:
        if (self.repo_dir / "lakefile.toml").exists():
            return self._parse_lakefile_toml_dependencies()
        else:
            return self._parse_lakefile_lean_dependencies()

    def _parse_lakefile_toml_dependencies(self) -> dict[str, tuple[GitURL, GitRev]]:
        contents = (self.repo_dir / "lakefile.toml").read_text()
        result = dict[str, tuple[GitURL, str]]()
        # Take all lines between each `[[require]]` and the next empty line.
        req_block_regex = r"(?<=\[\[require\]\])   .+   (?=\n\n)"
        for req in re.finditer(req_block_regex, contents, re.VERBOSE | re.DOTALL):
            m = dict[str, str]()
            for line in req.group().strip().splitlines():
                key, value = line.split("=")
                m[key.strip()] = value.strip()
            if "path" in m:
                raise ValueError("Local dependencies are not supported.")
            result[m["name"]] = (m["url"], m.get("rev") or "HEAD")
        return result

    def _parse_lakefile_lean_dependencies(self) -> dict[str, tuple[GitURL, GitRev]]:
        contents = (self.repo_dir / "lakefile.lean").read_text()
        # Local dependencies are not supported, they look like `require ... from "..."`.
        if re.search(r"require \s+ \S+ \s+ from \"", contents, re.VERBOSE):
            raise ValueError("Local dependencies are not supported.")
        # Ex.: "require mathlib from git "https://github.com/leanprover-community/mathlib4" @ "v4.18.0-rc1"
        git_req_regex = r"""
            require  \s+  (?P<name>\S+)  \s+
            from  \s+  git  \s+ \"(?P<url>.+?)\"
            (\s+  @  \s+  \"(?P<rev>\S+)\")?
        """
        return {m["name"]: (m["url"], m["rev"] or "HEAD") for m in re.finditer(git_req_regex, contents, re.VERBOSE)}

    def get_license(self) -> str | None:
        """Return the contents of the `LICENSE` file (or None if there is none)."""
        if (self.repo_dir / "LICENSE").exists():
            return (self.repo_dir / "LICENSE").read_text()
        else:
            return None


class Pos(NamedTuple):
    """
    Position in source files (1-based line & column (Unicode codepoint) indices, consistent with IDEs).

    A position can refer to a final newline of a line,
    or EOF (the non-existing character after the last character (newline or not) of the last line).
    """
    line_nb: int
    column_nb: int

    def __str__(self) -> str:
        return f"({self.line_nb}, {self.column_nb})"

    def __repr__(self) -> str:
        return f"({self.line_nb}, {self.column_nb})"

    @classmethod
    def from_str(cls, s: str) -> Self:
        """Construct from a string representation like `(323, 1109)`."""
        line, column = s.removeprefix("(").removesuffix(")").split(",")
        return cls(int(line), int(column))

    @classmethod
    def range_from_str(cls, s: str) -> tuple[Self, Self]:
        s = s.removeprefix("(").removeprefix("(").removesuffix(")").removesuffix(")")
        s_line, s_col, e_line, e_col = s.split(",")
        s_col = s_col.strip().removesuffix(")")
        e_line = e_line.strip().removeprefix("(")
        return cls(int(s_line), int(s_col)), cls(int(e_line), int(e_col))


class LeanFile:
    """A Lean source file (`*.lean`)."""

    def __init__(self, repo: LeanGitRepo, path: Path) -> None:
        """
        Args:
        - repo: The Lean Git repo this file belongs to.
        - path: relative to the root directory of the repo.
        """
        self.repo = repo
        self.path = path
        assert self.path.suffix == ".lean", f"File extension must be .lean: {self.path}"
        assert not self.path.is_absolute(), f"Path must be relative (to the repo's root directory): {self.path}"

        self.lines = list[str]()
        """Raw source code as a list of lines (with newlines, except possibly on the last line), indexed from 0."""

        self.num_bytes_of_line = []
        """The number of bytes of each line (indexed from 0), including newlines."""

        for line in self.abs_path.open("rb"):
            if b"\r\n" in line:
                raise RuntimeError(
                    f"{self.abs_path} contains Windows-style line endings. "
                    + "This is discouraged (see https://github.com/leanprover-community/mathlib4/pull/6506)."
                )
            self.num_bytes_of_line.append(len(line))
            self.lines.append(line.decode("utf-8"))

    @property
    def abs_path(self) -> Path:
        """Absolute path of this file."""
        return self.repo.repo_dir / self.path

    @property
    def start_pos(self) -> Pos:
        """Start position of a source file: (1, 1)."""
        return Pos(1, 1)

    @property
    def end_pos(self) -> Pos:
        """End position of a source file (_after_ the last codepoint)."""
        # Line and column numbers are 1-based.
        line_nb = len(self.lines)
        column_nb = 1 + len(self.lines[-1])
        return Pos(line_nb, column_nb)

    def __str__(self) -> str:
        return f"LeanFile({self.repo.friendly_name}, {self.path!s})"

    def is_empty(self) -> bool:
        return bool(self.lines)

    def convert_pos(self, byte_idx: int) -> Pos:
        """
        Convert a 0-based byte index (`String.Pos` in Lean 4) to a lean_dojo `Pos` object.

        The byte_idx can point to just after the last character.
        Raises error if the byte_idx is greater or does not lie on an UTF-8 codepoint boundary.
        """
        if byte_idx == 0:  # Needed to handle empty files.
            return self.start_pos

        passed_n_bytes = 0
        for line_nb, line_n_bytes in enumerate(self.num_bytes_of_line, start=1):
            if byte_idx < passed_n_bytes + line_n_bytes:
                line_byte_idx = byte_idx - passed_n_bytes
                line = self.get_line(line_nb)
                line_bytes = line.encode("utf-8")
                assert len(line_bytes) == line_n_bytes
                assert 0 <= line_byte_idx < line_n_bytes
                return Pos(line_nb, 1 + len(line_bytes[:line_byte_idx].decode("utf-8")))

            passed_n_bytes += line_n_bytes

        if byte_idx == passed_n_bytes:
            return self.end_pos
        else:  # byte_idx > passed_n_bytes
            raise ValueError(f"Byte index {byte_idx} past end of file in {self.path}.")

    def offset(self, pos: Pos, delta: int) -> Pos:
        """Move a position forward by a given number of characters (Unicode codepoints)."""
        assert delta >= 0
        start_line_nb, start_column_nb = pos
        line_nb = start_line_nb
        line = self.get_line(line_nb)
        assert 1 <= start_column_nb <= len(line)
        line = line[: start_column_nb - 1]

        while True:
            if delta < len(line):
                return Pos(line_nb, start_column_nb + delta)
            else:
                delta -= len(line)
                line_nb += 1
                if line_nb > len(self.lines):
                    break
                line = self.get_line(line_nb)
                start_column_nb = 1

        if delta == 0:
            return self.end_pos

        raise ValueError(f"Invalid offset {delta} in {self.path}: {pos}.")

    def get_line(self, line_nb: int) -> str:
        """Return a line of the source file given its 1-based index."""
        return self.lines[line_nb - 1]

    def __getitem__(self, key: slice[Pos, Pos, None]) -> str:
        """Return a code segment given its start/end positions. This enables `lean_file[start:end]`."""
        assert isinstance(key, slice) and key.step is None
        start_line, start_column = key.start or self.start_pos
        end_line, end_column = key.stop or self.end_pos
        assert start_line > 0 and start_column > 0 and end_line > 0 and end_column >= 0
        if start_line == end_line:
            return self.get_line(start_line)[start_column - 1 : end_column - 1]
        elif start_line < end_line:
            code_slice = [self.lines[start_line - 1][start_column - 1 :]]
            for line_nb in range(start_line + 1, end_line):
                code_slice.append(self.get_line(line_nb))  # noqa: PERF401
            if end_column != 0:
                code_slice.append(self.get_line(end_line)[: end_column - 1])
            return "\n".join(code_slice)
        else:
            return ""


def get_lean4_dep_from_toolchain(lean_toolchain: str) -> tuple[GitURL, str]:
    """Return the required Lean repo url and commit given a ``lean-toolchain`` config."""
    # Get the version like `v4.18.0-rc1` or `nightly-2025-03-25`.
    if not lean_toolchain.startswith("leanprover/lean4:"):
        raise ValueError(f"Invalid lean-toolchain: {lean_toolchain!r}")
    v = lean_toolchain.removeprefix("leanprover/lean4:")
    if not v.startswith("v") and v[0].isnumeric():
        v = "v" + v

    if v.startswith("nightly-"):
        # hash = get_latest_commit(global_config.lean4_nightly_url, v)
        return (global_config.lean4_nightly_url, v)
    else:
        # hash = get_latest_commit(global_config.lean4_url, v)
        return (global_config.lean4_url, v)


def is_supported_version(v: str) -> bool:
    """Check if `v` is at least `v4.3.0-rc2`."""
    if not v.startswith("v"):
        return False
    v = v.removeprefix("v")
    major, minor, patch = [int(x) for x in v.split("-")[0].split(".")]
    rc = int(v.split("-rc")[1]) if "-rc" in v else 9999
    return (major, minor, patch, rc) >= (4, 3, 0, 2)


@dataclass(frozen=True)
class Theorem:
    """
    Theorem in a Lean file.

    Theorems are named constants of type `Prop`: typically using keywords `theorem` or `lemma`,
    but many others like `def` and `instance` are possible.
    """

    repo: LeanGitRepo
    """Lean repo the theorem comes from."""

    file_path: Path
    """Lean source file the theorem comes from."""

    full_name: str
    """Fully qualified name of the theorem."""

    def __post_init__(self) -> None:
        if isinstance(self.file_path, str):
            object.__setattr__(self, "file_path", Path(self.file_path))
        assert self.file_path.suffix == ".lean", f"File extension must be .lean: {self.file_path}"

    @property
    def uid(self) -> str:
        """Unique identifier of the theorem."""
        return f"{cleanse_string(self.repo.url)}@{cleanse_string(self.repo.commit)}:{cleanse_string(self.file_path.__str__())}:{cleanse_string(self.full_name)}"

    @property
    def uhash(self) -> str:
        """Unique hash of the theorem."""
        return str(hash(self.uid) ** 2)
