"""Download and install the current DLoop version into the current project.

This distribution entry point installs v3.8.1 without changing its payload.
It can run from stdin; --archive also supports a previously downloaded ZIP.
"""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile


VERSION = "3.8.1"
ARCHIVE_REF = "installer-v3.8.1-r2"
ARCHIVE_URL = f"https://codeload.github.com/qwofa/DLoop/zip/refs/tags/{ARCHIVE_REF}"


def run(*args, creationflags=0, cwd=None, input_data=None):
    result = subprocess.run(args, capture_output=True, creationflags=creationflags, cwd=cwd, input=input_data)
    if result.returncode:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise RuntimeError(detail.strip() or f"Command failed: {args[0]}")
    return result.stdout


def project_vcs(target):
    for directory in (target, *target.parents):
        if (directory / ".git").exists():
            run("git", "-C", str(target), "rev-parse", "--show-toplevel")
            return "git"
        if (directory / ".svn").exists():
            run("svn", "info", "--xml", ".", cwd=target)
            return "svn"
    raise RuntimeError("Open a Git or SVN project directory before installing DLoop.")


def prepare_ignore(target, vcs):
    """Return a rollback action only when project preparation changed a rule."""
    if vcs == "git":
        checked = subprocess.run(
            ["git", "-C", str(target), "check-ignore", "-q", "--", ".scratch/"],
            capture_output=True,
        )
        if checked.returncode == 0:
            return lambda: None
        if checked.returncode != 1:
            raise RuntimeError(checked.stderr.decode("utf-8", errors="replace"))
        path = target / ".gitignore"
        original = path.read_bytes() if path.exists() else None
        content = original or b""
        newline = b"\r\n" if b"\r\n" in content else b"\n"
        separator = newline if content and not content.endswith(b"\n") else b""
        path.write_bytes(content + separator + b"/.scratch/" + newline)

        def restore():
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

        return restore

    import xml.etree.ElementTree as ET

    properties = ET.fromstring(run("svn", "proplist", "--xml", ".", cwd=target))
    exists = properties.find(".//property[@name='svn:ignore']") is not None
    original = run("svn", "propget", "--strict", "svn:ignore", ".", cwd=target) if exists else b""
    if b".scratch" in original.splitlines():
        return lambda: None
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    separator = newline if original and not original.endswith(b"\n") else b""
    run("svn", "propset", "svn:ignore", "--file", "-", ".", cwd=target,
        input_data=original + separator + b".scratch" + newline)

    def restore():
        if exists:
            run("svn", "propset", "svn:ignore", "--file", "-", ".", cwd=target, input_data=original)
        else:
            run("svn", "propdel", "svn:ignore", ".", cwd=target)

    return restore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=Path.cwd())
    parser.add_argument("--archive", type=Path, help="Use a downloaded v3.8.1 source ZIP")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("DLoop currently requires Windows.")
    target = args.target.resolve(strict=True)
    if not target.is_dir():
        raise RuntimeError("The target must be a project directory.")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("PowerShell is required to install DLoop.")
    vcs = project_vcs(target)
    with tempfile.TemporaryDirectory(prefix="dloop-install-") as temporary:
        scratch = Path(temporary)
        archive = args.archive
        if archive is None:
            archive = scratch / "dloop.zip"
            print(f"Downloading DLoop {VERSION}...", flush=True)
            urllib.request.urlretrieve(ARCHIVE_URL, archive)
        source_root = scratch / "source"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(source_root)
        installers = list(source_root.glob("*/install.ps1"))
        if len(installers) != 1:
            raise RuntimeError("The archive must contain one DLoop release directory.")
        source = installers[0].parent
        if (source / "VERSION").read_text(encoding="utf-8").strip() != VERSION:
            raise RuntimeError(f"This entry point installs DLoop {VERSION} only.")
        installer_path = str(installers[0]).replace("'", "''")
        target_path = str(target).replace("'", "''")
        command = [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                   "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                   f"& '{installer_path}' -Target '{target_path}' -Version 'v{VERSION}'"]
        restore_ignore = prepare_ignore(target, vcs)
        try:
            print(f"Installing DLoop {VERSION} into {target}...", flush=True)
            # Keep UTF-8 output local to the child, without changing the caller's console.
            print(run(*command, creationflags=subprocess.CREATE_NO_WINDOW).decode("utf-8", errors="replace").strip())
        except BaseException:
            restore_ignore()
            raise
        # A failed read-only verification must not remove a rule needed by an
        # installation that has already completed successfully.
        run(*command, "-Verify", creationflags=subprocess.CREATE_NO_WINDOW)
    print("DLoop installed and verified.")
    print("Open this project in Codex. Use $dloop or $dloop-ui to start.")
    print("Unity and its MCP connection must already be configured in Codex.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"DLoop installation failed: {error}", file=sys.stderr)
        sys.exit(1)
