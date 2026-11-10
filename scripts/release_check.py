"""Validate a release wheel and write SHA256SUMS without publishing anything."""
import argparse
import hashlib
import re
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def check_release(project, dist, tag=""):
    version = tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Release versions must use MAJOR.MINOR.PATCH.")
    if tag and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} does not match project version v{version}.")
    wheels = sorted(dist.glob("*.whl"))
    expected = f"local_agent_cli-{version}-py3-none-any.whl"
    if [wheel.name for wheel in wheels] != [expected]:
        raise ValueError(f"Expected only {expected} in {dist}.")
    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read(f"local_agent_cli-{version}.dist-info/METADATA"))
        if metadata["Name"] != "local-agent-cli" or metadata["Version"] != version:
            raise ValueError("Wheel metadata does not match the project name and version.")
        required = {"local_agent/__main__.py", "local_agent/tui/app.py", "local_agent/tui/app.tcss"}
        if not required.issubset(archive.namelist()):
            raise ValueError("Wheel is missing the command entry point or terminal UI assets.")
        source = project.parent / "local_agent"
        expected_files = {path.relative_to(project.parent).as_posix() for path in source.rglob("*")
                          if path.is_file() and path.suffix in {".py", ".tcss"}}
        packaged_files = {name for name in archive.namelist() if name.startswith("local_agent/")}
        if packaged_files != expected_files:
            raise ValueError("Wheel package files differ from the source tree. Build in a clean checkout "
                             "or remove the old build directory first.")
        if archive.testzip() is not None:
            raise ValueError("Wheel contains a corrupt file.")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = dist / "SHA256SUMS"
    manifest.write_text(f"{digest}  {wheel.name}\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="Require this tag to match pyproject.toml (vMAJOR.MINOR.PATCH)")
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        manifest = check_release(ROOT / "pyproject.toml", args.dist, args.tag)
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Release check failed: {exc}\n")
    print(manifest.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":
    main()
