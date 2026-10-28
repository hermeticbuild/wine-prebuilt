#!/usr/bin/env python3
"""Package a runtime together with its notices and corresponding source recipe."""

import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

from fetch import ROOT
from distribution import FORMATS, PLATFORMS, TOOLS, TOOLS_PLATFORMS, basename, sha256, version


def normalized(info):
    if "__pycache__" in Path(info.name).parts or info.name.endswith(".pyc"):
        return None
    info.uid = info.gid = info.mtime = 0
    info.uname = info.gname = ""
    info.pax_headers = {}
    return info


def archive(destination, entries):
    """Compress one normalized tar stream into both release formats."""
    zstd = destination.with_name(destination.name.removesuffix(".tar.gz") + ".tar.zst")
    with tempfile.TemporaryFile(dir=destination.parent) as tar:
        with tarfile.open(fileobj=tar, mode="w|", dereference=False) as output:
            for path, name in entries:
                output.add(path, arcname=name, filter=normalized)
        tar.seek(0)
        with destination.open("xb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
                shutil.copyfileobj(tar, compressed)
        tar.seek(0)
        with zstd.open("xb") as raw:
            subprocess.run(["zstd", "-q", "-T1", "-c"], stdin=tar, stdout=raw, check=True)


def copy_notices(source, destination):
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")):
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("build", type=Path, help="Build directory containing install/, sources/, recipe/ and build.json")
    parser.add_argument("--serial", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "out")
    args = parser.parse_args()
    if args.serial < 0:
        parser.error("--serial must be nonnegative")
    directory = args.build.resolve()
    build = json.loads((directory / "build.json").read_text())
    product = build.get("product", "runtime")
    runtime = directory / "install"
    name = basename(build["platform"], build["wine"], args.serial, product)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"{name}.tar.gz"
    sources = output / f"{name}-sources.tar.gz"
    paths = [output / (name + kind + extension) for kind in ("", "-sources") for extension in FORMATS]
    if any(path.exists() for path in paths):
        parser.error("artifact already exists; use a new output directory or serial")
    manifest = {key: value for key, value in build.items() if key not in ("install", "toolchain", "sysroot")}
    os_name, cpu = build["platform"].split("-")
    manifest.update(schema_version=1, product=product, serial=args.serial, artifact_version=version(build["wine"], args.serial),
                    executor={"os": os_name, "cpu": cpu},
                    source_archives=[name + "-sources" + extension for extension in FORMATS])
    if product == "runtime":
        manifest.update(windows_cpus=PLATFORMS[build["platform"]]["guests"],
                        requirements=PLATFORMS[build["platform"]]["requirements"],
                        executables={"wine": "bin/wine", "wineserver": "bin/wineserver"})
    else:
        suffix = ".exe" if os_name == "windows" else ""
        manifest.update(requirements=TOOLS_PLATFORMS[build["platform"]],
                        executables={tool: "bin/" + tool + suffix for tool in TOOLS},
                        target_cpus=["amd64", "arm64"],
                        include_dir="include/wine/windows", typelib_dir="lib/wine",
                        nls_dir=None if os_name == "windows" else "share/wine/nls",
                        features={"gettext_po_output": False, "locale_data": "windows" if suffix else "bundled"})
    with tempfile.TemporaryDirectory(prefix="package-", dir=directory) as temporary:
        staging = Path(temporary) / name
        # Hard links avoid duplicating large PE trees. No existing file is edited.
        shutil.copytree(runtime, staging, symlinks=True, copy_function=os.link)
        notices = staging / "LICENSES"
        copy_notices(directory / "sources/wine", notices / "wine")
        if build["toolchain"]:
            copy_notices(Path(build["toolchain"]), notices / "llvm-mingw")
        if build["sysroot"]:
            copy_notices(Path("/usr/share/doc/gcc-13-base"), notices / "gcc-host")
            if build["platform"] == "linux-amd64":
                sysroot = Path(build["sysroot"])
                copy_notices(sysroot / "usr/share/doc/gcc-4.8.5", notices / "gcc-sysroot")
        source_entries = [(directory / "sources/wine", "wine")]
        if build["fex"]:
            copy_notices(directory / "sources/fex", notices / "fex")
            source_entries.append((directory / "sources/fex", "fex"))
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        shutil.copy2(directory / "recipe/DISTRIBUTION.md", staging / "DISTRIBUTION.md")
        archive(artifact, [(staging, ".")])
        source_entries.append((directory / "recipe", "recipe"))
        source_entries.append((staging / "manifest.json", "manifest.json"))
        archive(sources, source_entries)
        shutil.copyfile(staging / "manifest.json", output / f"{name}.manifest.json")
    for path in paths:
        path.with_name(path.name + ".sha256").write_text(f"{sha256(path)}  {path.name}\n")
    print(artifact)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"artifact={artifact}\nattestation_name={name}.attestation.jsonl\n")


if __name__ == "__main__":
    main()
