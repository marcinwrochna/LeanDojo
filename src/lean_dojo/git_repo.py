import os
import re
import string
import subprocess
import urllib.parse
from functools import cache
from pathlib import Path

from filelock import FileLock
from loguru import logger

from .constants import global_config

GitURL = str | Path
"""URL, local path, ssh-like `user@host:path`, or anything else that `git clone/ls-remote` accepts."""

GitRev = str
"""Commit hash, or tag/branch/ref (in which case the last commit is used, which can change, prefer hashes)."""


def load_cached_repo_clone(
    repo: GitURL, rev: GitRev, cache_dir: Path | None = None, *, sparse: bool = False, assert_cached: bool = False
) -> Path:
    """
    Find a cached copy of the repo from the cache directory, or clone and cache it if not found.

    Args:
    - repo: URL, ssh-style `user@host:path`, local path, or anything else that `git clone` an `git ls-remote` accepts.
    - rev: commit hash, or tag/branch/ref (in which case the last commit is used, which can change, prefer hashes).
        If cloning, we do a shallow clone of this commit only, with no history, in a 'detached HEAD' state.
    - sparse: if True, only files in the toplevel directory are checked-out (unless the repo's already there).
        To complete it to a full repo (for a single commit), call again with sparse=False,
        or run `git sparse-checkout disable`.
    - cache_dir: directory under which repos will be cached; will be created with parents if needed.
        Defaults to global_config.cache_dir
    - assert_cached: if True, never download/clone, instead raise an error if the repo is not found in the cache.

    If `repo` is a local path (or `file://` URL), we just return the path, verifying its commit hash,
    and unsparsing if needed.
    Otherwise the path to the cached repo (`cache_dir / get_friendly_repo_name(repo) / commit_hash` ) is returned.

    Uses the `GITHUB_ACCESS_TOKEN` env var if it is set and `repo` is a GitHub HTTPS URL without password.
    """
    if cache_dir is None:
        cache_dir = global_config.cache_dir
    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(repo, str) and repo.startswith("file://"):
        repo = _file_url_to_path(repo)

    hash = rev if _is_commit_hash(rev) else get_latest_commit(repo, rev)

    if isinstance(repo, Path):
        verify_and_unsparse_local_repo(repo, hash, sparse=sparse)
        return repo

    repo_without_pass = str(_redact_url_password(repo))
    cache_repo_dir = cache_dir / get_friendly_repo_name(repo_without_pass) / hash

    with FileLock(cache_dir / ".lock"):
        if cache_repo_dir.exists():
            verify_and_unsparse_local_repo(cache_repo_dir, hash, sparse=sparse)
            return cache_repo_dir

        if assert_cached:
            raise AssertionError(f"Cache for repo {repo_without_pass} does not exist: {cache_repo_dir}")

        # Download and cache the repo.
        # GitHub doesn't support `git archive`, so we have to clone.
        download_git_commit(repo, hash, cache_repo_dir, sparse=sparse)
        verify_and_unsparse_local_repo(cache_repo_dir, hash, sparse=sparse)
        return cache_repo_dir


def download_git_commit(repo: GitURL, rev: GitRev, target_dir: Path, sparse: bool = False) -> None:
    """
    Download a single commit from a git repo into `target_dir`.

    Args:
    - repo: URL, ssh-style `user@host:path`, local path, or anything else that `git clone` an `git ls-remote` accepts.
    - rev: commit hash, or tag or branch (tags and branches can change, prefer commit hashes).
        If cloning, we do a shallow clone of this commit only, with no history, in a 'detached HEAD' state.
    - target_dir: directory to clone into; will be created with if needed, otherwise must be empty.
    - sparse: if True, only files in the toplevel directory are checked-out (unless the repo's already there).
        To complete it to a full repo (for a single commit), call `verify_and_unsparse_local_repo` with sparse=False,
        or run `git sparse-checkout disable`.
    """
    hash = rev if _is_commit_hash(rev) else get_latest_commit(repo, rev)
    repo_without_pass = str(_redact_url_password(repo))

    assert get_git_version() >= (2, 49, 0), f"Git too old: {get_git_version()} < 2.49.0."
    # TODO support older git by using `--single-branch/--no-checkout` and `git checkout`.

    cmd = [
        "git",
        "clone",
        "--depth=1",
        "--recurse-submodules",
        "--shallow-submodules",
        "-c",
        "advice.detachedHead=false",  # We fetch only one commit as a detached HEAD, don't warn about it.
    ]
    if sparse:
        cmd += ["--sparse"]
    cmd += [f"--revision={hash}", "--", str(repo), str(target_dir)]
    cmd_without_pass = [c.replace(str(repo), repo_without_pass) for c in cmd]

    logger.info(f"Cloning {repo_without_pass} to {target_dir} ...")
    p = subprocess.run(cmd, capture_output=True, encoding="utf-8", check=False)
    if p.returncode:
        raise RuntimeError(f"Clone failed:\n{' '.join(cmd_without_pass)}\n{p.stdout}\n{p.stderr}")


@cache
def get_latest_commit(repo: GitURL, rev: GitRev = "HEAD") -> str:
    """
    Get the hash of the latest commit with given tag or branch (using `git ls-remote`).

    Uses the `GITHUB_ACCESS_TOKEN` env var if it is set and `repo` is a GitHub HTTPS URL without password.

    The answer is cached until python exits (for each repo and rev).

    The default rev="HEAD" for local repos means the currently checked-out commit (there may be local modifications).
    For remote repos it means the last commit of the default branch, e.g. "main" or "master".
    """
    repo = _fill_github_url_with_access_token(repo)
    cmd = ["git", "ls-remote", str(repo), rev]
    p = subprocess.run(cmd, capture_output=True, check=True, encoding="utf-8")
    if p.stderr:
        raise RuntimeError(f"Failed to get commit hash for {_redact_url_password(repo)} {rev}: {p.stderr}\n{p.stdout}")

    # Only get the line we actually want:
    # (If we ask for e.g. 'HEAD', we might get 'HEAD' (of local repo) and 'refs/remotes/origin/HEAD').
    for line in p.stdout.strip().splitlines():
        if line.endswith(f"\t{rev}"):
            hash = line.removesuffix(f"\t{rev}")
            assert _is_commit_hash(hash)
            return hash

    raise RuntimeError(f"Failed to get commit hash for {_redact_url_password(repo)} {rev}, got:\n{p.stdout}")


def verify_and_unsparse_local_repo(repo: Path, commit_hash: str, sparse: bool) -> None:
    """
    Assert given dir contains a git repo at the given commit hash; unsparse it if necessary.

    Args:
    - repo: directory containing a git repo.
    - commit_hash: commit hash we expect the repo to be at.
    - sparse: if False and the repo is sparse, unsparse it by checking out all files.
        If True, nothing is modified.
    """
    actual_hash = get_latest_commit(repo, "HEAD")
    if actual_hash != commit_hash:
        raise RuntimeError(f"Repo {repo} is on commit {actual_hash} != {commit_hash}")

    # Check the local repo has no uncommitted changes, inc. submodules, exc. untracked files.
    # `git diff HEAD` will report both unstaged and staged changes.
    p = subprocess.run(["git", "diff", "--quiet", "--ignore-submodules=none", "HEAD"], cwd=repo, check=False)
    if p.returncode:
        raise RuntimeError(f"Repo {repo} has uncommitted changes.")

    if sparse:
        return

    # Disable git sparse-checkout. This shouldn't change anything if it wasn't enabled.
    # The first command downloads 100% of the repo and submodules, but does not disable sparse-checkout.
    # The latter command perhaps does not recurse into submodules (as of git 2.49.0).
    for cmd in [
        ["git", "checkout", "--ignore-skip-worktree-bits", "--recurse-submodules", "."],
        ["git", "sparse-checkout", "disable"],
    ]:
        q = subprocess.run(cmd, capture_output=True, encoding="utf-8", cwd=repo, check=False)
        if q.returncode:
            raise RuntimeError(
                f"Failed to disable sparse-checkout in git repo {repo}\n"
                + f"stderr='''{q.stderr}'''\nstdout='''{q.stdout}'''"
            )
        m = re.search(r"Updated (\d+) paths from the index", q.stderr)
        if m and m.group(1) != "0":
            logger.info(f"Checked-out a sparse git repo to unsparse it ({m.group(1)} paths): {repo}")


def _fill_github_url_with_access_token(url: GitURL) -> GitURL:
    """If given a GitHub HTTPS URL without password but GITHUB_ACCESS_TOKEN is set in env, use it."""
    GITHUB_ACCESS_TOKEN = os.getenv("GITHUB_ACCESS_TOKEN", None)
    if GITHUB_ACCESS_TOKEN and isinstance(url, str):
        p = urllib.parse.urlparse(url)
        if p.scheme == "https" and p.hostname == "github.com" and p.port is None and p.password is None:
            return _replace_url_user_and_password(url, p.username or "x-access-token", GITHUB_ACCESS_TOKEN)
    return url


def get_friendly_repo_name(repo: GitURL) -> str:
    """
    Get a string like "username_reponame" that can be used in filesystem paths.

    This should not be assumed to be unique, use the commit hash for that.

    The string will be the same for SSH-like Git URLs like `git@github.com:user/repo.git` and
    the corresponding HTTPS URL like `https://github.com/user/repo.git`.
    The .git suffix is and trailing slashes are always removed.

    The result only contains alphanumeric characters (`str.isalnum()`, including UTF),
    underscores _, dots ., and ASCII hyphens -; anything else is replaced by underscores.
    An initial dot is also replaced by an underscore.
    """
    if isinstance(repo, Path):
        name = repo.parent.name + "_" + repo.name
    else:
        # If repo is a SSH-like string like 'git@github.com:user/repo.git', use f"{user}_{repo}".
        pattern = r"[~\w_.-]+  @  [\w_.-]+  :  (?P<user>[~\w_.-]+)  /  (?P<repo>[~\w_.-]+?)  (\.git)?"
        m = re.fullmatch(pattern, repo, flags=re.VERBOSE)
        if m:
            name = m.group("user") + "_" + m.group("repo")
        else:
            # Otherwise skip user/password/hostname etc. and just use the path portion of the URL.
            name = urllib.parse.urlparse(repo).path

    name = re.sub(r"/+", "/", name).removeprefix("/").removesuffix("/").removesuffix(".git")
    name = re.sub(r"[^\w_.-]+", "_", name)
    if name.startswith("."):
        name = "_" + name[1:]
    return name


def _is_commit_hash(s: GitRev) -> bool:
    """Check if a string is a 40-character hex value."""
    return len(s) == 40 and all(c in string.hexdigits.lower() for c in s)


def _file_url_to_path(url: str) -> Path:
    parsed_url = urllib.parse.urlparse(url)
    assert parsed_url.scheme in ("file", "")
    return Path(parsed_url.path)


def _replace_url_user_and_password(url: str, new_user: str | None, new_password: str | None) -> str:
    """Replace the `user:pass@` portion of an URL."""
    try:
        p = urllib.parse.urlparse(url)

        host_and_port = p.netloc
        if p.username:
            host_and_port = host_and_port.removeprefix(p.username)
            if p.password:
                host_and_port = host_and_port.removeprefix(":" + p.password)
            host_and_port = host_and_port.removeprefix("@")

        if new_password is not None:
            assert new_user is not None, f"E {new_user=!r} {new_password=!r} {p}"
            new_netloc = new_user + ":" + new_password + "@" + host_and_port
        elif new_user is not None:
            new_netloc = new_user + "@" + host_and_port
        else:
            new_netloc = host_and_port
        new_url = p._replace(netloc=new_netloc).geturl()

        p = urllib.parse.urlparse(new_url)
        assert p.username == new_user
        assert p.password == new_password
        return new_url
    except ValueError:
        return url


def _redact_url_password(url: GitURL) -> GitURL:
    """Replace the `user:pass@` portion of an URL with `user:***@`, if there is one."""
    if isinstance(url, Path):
        return url
    try:
        p = urllib.parse.urlparse(url)
        if p.password:
            return _replace_url_user_and_password(url, p.username, "***")
        return url
    except ValueError:
        return url


@cache
def get_git_version() -> tuple[int, int, int]:
    """Check the version of Git installed on the system."""
    res = subprocess.run("git --version", shell=True, capture_output=True, check=True)
    output = res.stdout.decode().strip()
    error = res.stderr.decode()
    assert error == "", error
    m = re.search(r"git version (\d+\.\d+\.\d+)", output)
    assert m, f"Could not parse Git version from: {output}"
    major, minor, patch = m.group(1).split(".")
    return (int(major), int(minor), int(patch))





def example() -> None:
    print(
        "A",
        load_cached_repo_clone(
            "https://github.com/yangky11/miniF2F-lean4",
            "bfc55b45af17e4ed77fd4ba3cc2bdc2d942ce99b",
            sparse=True,
            cache_dir=Path("~/tmp/repocache"),
        ),
    )
    print("X")
    print(
        "B",
        load_cached_repo_clone(
            "git@github.com:yangky11/miniF2F-lean4.git", "HEAD", sparse=False, cache_dir=Path("~/tmp/repocache")
        ),
    )


if __name__ == "__main__":
    example()
