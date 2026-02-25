#!/usr/bin/env python3
"""Install a skill from a GitHub repo. SSL-friendly + git fallback for old Git.

Same CLI as bub skill-installer; agent runs: uv run scripts/install-skill-from-github.py
--repo owner/repo --path path/to/skill.
"""

from __future__ import annotations

import argparse
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass

DEFAULT_REF = "main"


@dataclass
class Args:
    url: str | None = None
    repo: str | None = None
    path: list[str] | None = None
    ref: str = DEFAULT_REF
    dest: str | None = None
    name: str | None = None
    method: str = "auto"


@dataclass
class Source:
    owner: str
    repo: str
    ref: str
    paths: list[str]
    repo_url: str | None = None


class InstallError(Exception):
    pass


def _skills_home() -> str:
    if custom_root := os.environ.get("BUB_SKILLS_HOME"):
        return os.path.expanduser(custom_root)
    if bub_home := os.environ.get("BUB_HOME"):
        return os.path.join(os.path.expanduser(bub_home), "skills")
    return os.path.expanduser("~/.agent/skills")


def _tmp_root() -> str:
    base = os.path.join(tempfile.gettempdir(), "bub")
    os.makedirs(base, exist_ok=True)
    return base


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    for env in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(env)
        if path and os.path.isfile(path):
            ctx.load_verify_locations(path)
            break
    return ctx


def _request(url: str) -> bytes:
    headers = {"User-Agent": "bub-skill-install"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as resp:
        return resp.read()


def _parse_github_url(url: str, default_ref: str) -> tuple[str, str, str, str | None]:
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc != "github.com":
        raise InstallError("Only GitHub URLs are supported for download mode.")
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise InstallError("Invalid GitHub URL.")
    owner, repo = parts[0], parts[1]
    ref = default_ref
    subpath = ""
    if len(parts) > 2:
        if parts[2] in ("tree", "blob"):
            if len(parts) < 4:
                raise InstallError("GitHub URL missing ref or path.")
            ref = parts[3]
            subpath = "/".join(parts[4:])
        else:
            subpath = "/".join(parts[2:])
    return owner, repo, ref, subpath or None


def _download_repo_zip(owner: str, repo: str, ref: str, dest_dir: str) -> str:
    zip_url = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    zip_path = os.path.join(dest_dir, "repo.zip")
    try:
        payload = _request(zip_url)
    except urllib.error.HTTPError as exc:
        raise InstallError(f"Download failed: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise InstallError(f"Download failed: {exc.reason}") from exc
    with open(zip_path, "wb") as f:
        f.write(payload)
    with zipfile.ZipFile(zip_path, "r") as zf:
        _safe_extract_zip(zf, dest_dir)
        top_levels = {n.split("/")[0] for n in zf.namelist() if n}
    if not top_levels:
        raise InstallError("Downloaded archive was empty.")
    if len(top_levels) != 1:
        raise InstallError("Unexpected archive layout.")
    return os.path.join(dest_dir, next(iter(top_levels)))


def _run_git(args: list[str]) -> None:
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise InstallError(r.stderr.strip() or "Git command failed.")


def _safe_extract_zip(zip_file: zipfile.ZipFile, dest_dir: str) -> None:
    dest_root = os.path.realpath(dest_dir)
    for info in zip_file.infolist():
        extracted = os.path.realpath(os.path.join(dest_dir, info.filename))
        if extracted == dest_root or extracted.startswith(dest_root + os.sep):
            continue
        raise InstallError("Archive contains files outside the destination.")
    zip_file.extractall(dest_dir)


def _validate_relative_path(path: str) -> None:
    if os.path.isabs(path) or os.path.normpath(path).startswith(".."):
        raise InstallError("Skill path must be a relative path inside the repo.")


def _validate_skill_name(name: str) -> None:
    if not name or os.path.sep in name or (os.path.altsep and os.path.altsep in name):
        raise InstallError("Skill name must be a single path segment.")
    if name in (".", ".."):
        raise InstallError("Invalid skill name.")


def _git_sparse_checkout(repo_url: str, ref: str, paths: list[str], dest_dir: str) -> str:
    repo_dir = os.path.join(dest_dir, "repo")
    try:
        _run_git(
            [
                "git",
                "clone",
                "--filter=blob:none",
                "--depth",
                "1",
                "--sparse",
                "--single-branch",
                "--branch",
                ref,
                repo_url,
                repo_dir,
            ]
        )
        _run_git(["git", "-C", repo_dir, "sparse-checkout", "set", *paths])
        _run_git(["git", "-C", repo_dir, "checkout", ref])
        return repo_dir
    except InstallError:
        return _git_full_clone_then_copy(repo_url, ref, paths, dest_dir)


def _git_full_clone_then_copy(repo_url: str, ref: str, paths: list[str], dest_dir: str) -> str:
    """Fallback when git does not support --sparse: full clone then return repo root."""
    repo_dir = os.path.join(dest_dir, "repo")
    _run_git(["git", "clone", "--depth", "1", "--single-branch", "--branch", ref, repo_url, repo_dir])
    return repo_dir


def _validate_skill(path: str) -> None:
    if not os.path.isdir(path):
        raise InstallError(f"Skill path not found: {path}")
    if not os.path.isfile(os.path.join(path, "SKILL.md")):
        raise InstallError("SKILL.md not found in selected skill directory.")


def _copy_skill(src: str, dest_dir: str) -> None:
    os.makedirs(os.path.dirname(dest_dir), exist_ok=True)
    if os.path.exists(dest_dir):
        raise InstallError(f"Destination already exists: {dest_dir}")
    shutil.copytree(src, dest_dir)


def _build_repo_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}.git"


def _build_repo_ssh(owner: str, repo: str) -> str:
    return f"git@github.com:{owner}/{repo}.git"


def _prepare_repo(source: Source, method: str, tmp_dir: str) -> str:
    if method in ("download", "auto"):
        try:
            return _download_repo_zip(source.owner, source.repo, source.ref, tmp_dir)
        except InstallError:
            if method == "download":
                raise
            # auto: fall back to git on any download error (HTTP, SSL, timeout)
    if method in ("git", "auto"):
        repo_url = source.repo_url or _build_repo_url(source.owner, source.repo)
        try:
            return _git_sparse_checkout(repo_url, source.ref, source.paths, tmp_dir)
        except InstallError:
            repo_url = _build_repo_ssh(source.owner, source.repo)
            return _git_sparse_checkout(repo_url, source.ref, source.paths, tmp_dir)
    raise InstallError("Unsupported method.")


def _resolve_source(args: Args) -> Source:
    if args.url:
        owner, repo, ref, url_path = _parse_github_url(args.url, args.ref)
        paths = list(args.path) if args.path is not None else ([url_path] if url_path else [])
        if not paths:
            raise InstallError("Missing --path for GitHub URL.")
        return Source(owner=owner, repo=repo, ref=ref, paths=paths)
    if not args.repo:
        raise InstallError("Provide --repo or --url.")
    if "://" in args.repo:
        return _resolve_source(Args(url=args.repo, repo=None, path=args.path, ref=args.ref))
    parts = [p for p in args.repo.split("/") if p]
    if len(parts) != 2:
        raise InstallError("--repo must be in owner/repo format.")
    if not args.path:
        raise InstallError("Missing --path for --repo.")
    return Source(owner=parts[0], repo=parts[1], ref=args.ref, paths=list(args.path))


def _default_dest() -> str:
    return _skills_home()


def _parse_args(argv: list[str]) -> Args:
    p = argparse.ArgumentParser(description="Install a skill from GitHub.")
    p.add_argument("--repo", help="owner/repo")
    p.add_argument("--url", help="https://github.com/owner/repo[/tree/ref/path]")
    p.add_argument("--path", nargs="+", help="Path(s) to skill(s) inside repo")
    p.add_argument("--ref", default=DEFAULT_REF)
    p.add_argument("--dest", help="Destination skills directory")
    p.add_argument("--name", help="Destination skill name (defaults to basename of path)")
    p.add_argument("--method", choices=["auto", "download", "git"], default="auto")
    return p.parse_args(argv, namespace=Args())


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        source = _resolve_source(args)
        source.ref = source.ref or args.ref
        if not source.paths:
            raise InstallError("No skill paths provided.")
        for path in source.paths:
            _validate_relative_path(path)
        dest_root = os.path.expanduser(args.dest) if args.dest else _default_dest()
        tmp_dir = tempfile.mkdtemp(prefix="skill-install-", dir=_tmp_root())
        try:
            repo_root = _prepare_repo(source, args.method, tmp_dir)
            installed = []
            for path in source.paths:
                skill_name = args.name if len(source.paths) == 1 else None
                skill_name = skill_name or os.path.basename(path.rstrip("/"))
                _validate_skill_name(skill_name)
                if not skill_name:
                    raise InstallError("Unable to derive skill name.")
                dest_dir = os.path.join(dest_root, skill_name)
                if os.path.exists(dest_dir):
                    raise InstallError(f"Destination already exists: {dest_dir}")
                skill_src = os.path.join(repo_root, path)
                _validate_skill(skill_src)
                _copy_skill(skill_src, dest_dir)
                installed.append((skill_name, dest_dir))
        finally:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        for skill_name, dest_dir in installed:
            print(f"Installed {skill_name} to {dest_dir}")
        return 0
    except InstallError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
