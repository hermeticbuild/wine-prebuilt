#!/usr/bin/env python3
"""Build one executor-native, headless Wine distribution from pinned inputs."""

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile

from fetch import ROOT, fetch
from distribution import PLATFORMS, MACOS_DEPLOYMENT_TARGET

NATIVE_PLATFORMS = {
    "linux-amd64": ("Linux", "x86_64", "x86_64,i386"),
    "linux-arm64": ("Linux", "aarch64", "arm64ec,aarch64,i386"),
    "macos-arm64": ("Darwin", "arm64", "arm64ec,aarch64,i386"),
}


def run(*command, **kwargs):
    subprocess.run(command, check=True, **kwargs)


def snapshot_recipe(directory):
    """Retain the recipe alongside each product's corresponding source."""
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True))
    for name in ("tools", "tests", "patches", ".github", "sources.json", "Dockerfile",
                 "README.md", "DISTRIBUTION.md", "LICENSE", ".clang-format", ".gitignore"):
        target = directory / "recipe" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(ROOT / name, target)
    return {"os": platform.platform(), "recipe_commit": revision, "recipe_dirty": dirty}


def prepare_wine(destination, source, target):
    if destination.exists():
        return
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "source"
        shutil.copytree(source, staging, symlinks=True)
        if target == "macos-arm64":
            patches = sorted((ROOT / "patches/macos").glob("*.patch"))
        else:
            patches = [ROOT / "patches/linux-address-space-probe.patch"]
            if target == "linux-arm64":
                # The emulator registration is shared; Darwin adaptations are not.
                patches.append(ROOT / "patches/linux-arm64-fex.patch")
        for patch in patches:
            # Unlike git apply, patch does not depend on an enclosing worktree.
            run("patch", "-t", "-N", "-F", "0", "-p1", "-i", str(patch),
                cwd=staging, stdin=subprocess.DEVNULL)
        staging.rename(destination)


def prepare_fex(destination, cache):
    if destination.exists():
        return
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        staging = Path(temporary) / "source"
        shutil.copytree(fetch("fex", cache), staging, symlinks=True)
        for name in ("fmt", "range-v3", "unordered_dense", "xxhash", "rpmalloc", "cpp-optparse"):
            parent = "Source/Common" if name == "cpp-optparse" else "External"
            shutil.copytree(fetch("fex-" + name, cache), staging / parent / name,
                            dirs_exist_ok=True, symlinks=True)
        staging.rename(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=PLATFORMS)
    parser.add_argument("--work", type=Path, default=ROOT / ".work")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    system, cpu, architectures = NATIVE_PLATFORMS[args.platform]
    if (platform.system(), platform.machine()) != (system, cpu):
        parser.error(f"{args.platform} must build on native {system}/{cpu}")
    work = args.work.resolve()
    cache = work / "cache"
    directory = work / args.platform
    if directory.exists():
        parser.error(f"build directory already exists: {directory}; choose a fresh --work directory")
    directory.mkdir(parents=True)
    # Preserve the exact recipe files used, including local edits. Release
    # builds require a clean checkout; local experiments are marked dirty.
    build_environment = snapshot_recipe(directory)
    toolchain = fetch("toolchain-" + args.platform, cache)
    (directory / "sources").mkdir()
    source = directory / "sources/wine"
    prepare_wine(source, fetch("wine", cache), args.platform)
    fex = directory / "sources/fex"
    if cpu != "x86_64":
        print("Preparing required FEX ARM64EC and WoW64 modules", flush=True)
        prepare_fex(fex, cache)
    install = directory / "install"
    environment = os.environ.copy()
    for name in ("CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "SDKROOT",
                 "MACOSX_DEPLOYMENT_TARGET", "PKG_CONFIG_PATH", "CONFIG_SITE"):
        environment.pop(name, None)
    environment["PATH"] = str(toolchain / "bin") + os.pathsep + environment["PATH"]
    environment["JOBS"] = str(args.jobs)
    sources = json.loads((ROOT / "sources.json").read_text())
    sysroot = None
    if system == "Darwin":
        environment.update(CC="/usr/bin/clang", CXX="/usr/bin/clang++",
                           MACOSX_DEPLOYMENT_TARGET=MACOS_DEPLOYMENT_TARGET)
        bison = subprocess.check_output(["brew", "--prefix", "bison"], text=True).strip()
        environment["BISON"] = str(Path(bison) / "bin/bison")
        build_environment.update(
            deployment_target=MACOS_DEPLOYMENT_TARGET,
            sdk_version=subprocess.check_output(["xcrun", "--show-sdk-version"], text=True).strip(),
            sdk_build=subprocess.check_output(["xcrun", "--show-sdk-build-version"], text=True).strip(),
        )
    else:
        sysroot = Path(subprocess.check_output([
            sys.executable, str(ROOT / "tools/prepare-sysroot.py"), args.platform.removeprefix("linux-"),
            str(work / "sysroots"), "--cache", str(cache)], text=True).strip())
        flags = [f"--sysroot={sysroot}", f"-B{sysroot}/usr/lib64",
                 f"-L{sysroot}/lib64", "-static-libgcc"]
        environment["CC"] = shlex.join(["gcc", *flags])
        environment["CXX"] = shlex.join(["g++", *flags])
    print(f"Building {args.platform}: {directory}", flush=True)
    run("bash", str(ROOT / "tools/build-wine.sh"), str(source),
        str(directory / "wine-build"), str(install), architectures, env=environment)
    if cpu != "x86_64":
        print("Building required FEX ARM64EC and WoW64 modules", flush=True)
        run("bash", str(ROOT / "tools/build-fex.sh"), str(fex),
            str(directory / "fex-build"), str(install), env=environment)
    build_environment["compiler"] = subprocess.check_output(
        [*shlex.split(environment["CC"]), "--version"], text=True).strip()
    (directory / "build.json").write_text(json.dumps({
        "platform": args.platform, "wine": sources["wine"]["version"], "fex": cpu != "x86_64",
        "install": str(install), "toolchain": str(toolchain),
        "build": build_environment,
        "sources": sources,
        "sysroot": str(sysroot) if sysroot else None,
    }, indent=2) + "\n")
    print(install)


if __name__ == "__main__":
    main()
