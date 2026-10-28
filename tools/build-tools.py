#!/usr/bin/env python3
"""Build standalone Wine developer tools; Windows targets are cross-built on Unix."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys

from build import snapshot_recipe
from distribution import MACOS_DEPLOYMENT_TARGET, TOOLS, TOOLS_PLATFORMS
from fetch import ROOT, fetch

# Wine 11.20 tools/*/Makefile.in. Keep parser generation in the release build,
# rather than checking generated C into this repository or requiring it downstream.
SOURCES = {
    "wmc": "lang mcl po utils wmc write",
    "wrc": "genres newstruc po utils wpp wrc",
    "widl": "attribute client expr hash header metadata proxy register server typegen typelib typetree utils widl wpp write_msft write_sltg",
}
CPUS = {"amd64": "x86_64", "arm64": "aarch64"}


def compile_tools(source, directory, compiler, tools, bison, windows=False):
    directory.mkdir(parents=True)
    wine_version = json.loads((ROOT / "sources.json").read_text())["wine"]["version"]
    (directory / "config.h").write_text(
        f'#define __WINE_CONFIG_H\n#define PACKAGE_VERSION "{wine_version}"\n'
        '#define BINDIR "/usr/bin"\n#define DATADIR "/usr/share"\n'
        '#define INCLUDEDIR "/usr/include"\n#define LIBDIR "/usr/lib"\n')
    for tool in tools:
        output = directory / tool
        output.mkdir()
        sources = []
        for stem in SOURCES[tool].split():
            parent = "wrc" if tool == "widl" and stem == "wpp" else tool
            sources.append(source / "tools" / parent / (stem + ".c"))
        grammars = [(tool, "mcy")] if tool == "wmc" else [(tool, "parser"), ("wrc", "ppy")]
        for parent, stem in grammars:
            subprocess.run([bison, "-d", "-o", str(output / (stem + ".tab.c")),
                            str(source / "tools" / parent / (stem + ".y"))], check=True)
            sources.append(output / (stem + ".tab.c"))
        if tool != "wmc":
            for parent, stem in [(tool, "parser"), ("wrc", "ppl")]:
                subprocess.run(["flex", "-o" + str(output / (stem + ".yy.c")),
                                str(source / "tools" / parent / (stem + ".l"))], check=True)
                sources.append(output / (stem + ".yy.c"))
        includes = [output, source / "tools" / tool, source / "tools/wrc", directory, source / "include"]
        flags = ["-O2", "-D__WINESRC__", *["-I" + str(path) for path in includes]]
        if not windows:
            flags.append("-DWINE_UNIX_LIB")

        def compile_one(path):
            obj = output / (path.stem + ".o")
            with obj.with_suffix(".log").open("w") as log:
                subprocess.run([*compiler, *flags, "-c", str(path), "-o", str(obj)],
                               check=True, stdout=log, stderr=log)
            return obj

        with ThreadPoolExecutor(max_workers=4) as pool:
            objects = list(pool.map(compile_one, sources))
        executable = output / (tool + (".exe" if windows else ""))
        subprocess.run([*compiler, *map(str, objects), "-o", str(executable)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=TOOLS_PLATFORMS)
    parser.add_argument("--work", type=Path, default=ROOT / ".work")
    args = parser.parse_args()
    host_os = {"Linux": "linux", "Darwin": "macos"}[platform.system()]
    host_cpu = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
    host = host_os + "-" + host_cpu
    windows = args.platform.startswith("windows-")
    if not windows and args.platform != host:
        parser.error("Unix tools must build on their native executor")
    work = args.work.resolve()
    directory = work / ("tools-" + args.platform)
    if directory.exists():
        parser.error("build directory exists; choose a fresh --work directory")
    directory.mkdir(parents=True)
    metadata = snapshot_recipe(directory)
    source = directory / "sources/wine"
    shutil.copytree(fetch("wine", work / "cache"), source, symlinks=True)
    toolchain = fetch("toolchain-" + host, work / "cache")
    # Match the installed Wine headers to the native Windows compiler's CRT.
    if windows:
        subprocess.run(["patch", "-t", "-N", "-F", "0", "-p1", "-i",
                        str(ROOT / "patches/tools-mingw-fastfail.patch")], cwd=source, check=True)
    bison = os.environ.get("BISON", "bison")
    if host_os == "macos":
        bison = subprocess.check_output(["brew", "--prefix", "bison"], text=True).strip() + "/bin/bison"
    sysroot = None
    native_cc = ["/usr/bin/clang"] if host_os == "macos" else ["gcc"]
    if windows:
        cpu = CPUS[args.platform.split("-")[1]]
        compiler = [str(toolchain / "bin" / (cpu + "-w64-mingw32-clang"))]
    elif host_os == "linux":
        sysroot = Path(subprocess.check_output([
            sys.executable, str(ROOT / "tools/prepare-sysroot.py"), host_cpu,
            str(work / "sysroots"), "--cache", str(work / "cache")], text=True).strip())
        compiler = ["gcc", f"--sysroot={sysroot}", f"-B{sysroot}/usr/lib64",
                    f"-L{sysroot}/lib64", "-static-libgcc"]
    else:
        compiler = [*native_cc, "-mmacosx-version-min=" + MACOS_DEPLOYMENT_TARGET]
        metadata.update(deployment_target=MACOS_DEPLOYMENT_TARGET,
                        sdk_version=subprocess.check_output(["xcrun", "--show-sdk-version"], text=True).strip(),
                        sdk_build=subprocess.check_output(["xcrun", "--show-sdk-build-version"], text=True).strip())
    metadata.update(compiler=subprocess.check_output([*compiler, "--version"], text=True).strip(),
                    compiler_command=shlex.join(compiler),
                    bison=subprocess.check_output([bison, "--version"], text=True).splitlines()[0],
                    flex=subprocess.check_output(["flex", "--version"], text=True).strip())
    build = directory / "tools-build"
    compile_tools(source, build, compiler, TOOLS, bison, windows)
    if windows:
        # A native WIDL generates the target type libraries during cross-builds.
        helper = directory / "native-build"
        compile_tools(source, helper, native_cc, ["widl"], bison)
        widl = helper / "widl/widl"
    else:
        widl = build / "widl/widl"
    install = directory / "install"
    (install / "bin").mkdir(parents=True)
    for tool in TOOLS:
        filename = tool + (".exe" if windows else "")
        shutil.copy2(build / tool / filename, install / "bin" / filename)
    # These are compiler input definitions, not a replacement C/C++ Windows SDK.
    headers = install / "include/wine/windows"
    for path in sorted((source / "include").rglob("*")):
        if path.is_file() and path.suffix in (".h", ".rh", ".idl"):
            destination = headers / path.relative_to(source / "include")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
    if not windows:
        shutil.copytree(source / "nls", install / "share/wine/nls",
                        ignore=lambda path, names: [name for name in names if not name.endswith(".nls")])
    for cpu in CPUS.values():
        destination = install / "lib/wine" / (cpu + "-windows")
        destination.mkdir(parents=True)
        subprocess.run([str(widl), "--nostdinc", "-I" + str(headers), "-b", cpu + "-w64-mingw32",
                        "-t", "-o", str(destination / "stdole2.tlb"),
                        str(source / "dlls/stdole2.tlb/stdole2.idl")], check=True)
    sources = json.loads((ROOT / "sources.json").read_text())
    (directory / "build.json").write_text(json.dumps({
        "product": "tools", "platform": args.platform, "wine": sources["wine"]["version"],
        "fex": False, "install": str(install), "toolchain": str(toolchain),
        "sysroot": str(sysroot) if sysroot else None, "build": metadata, "sources": sources,
    }, indent=2) + "\n")
    print(install)


if __name__ == "__main__":
    main()
