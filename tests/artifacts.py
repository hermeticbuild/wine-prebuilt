#!/usr/bin/env python3
"""Check the distributed archives: layout, architecture, dependencies and ABI."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from distribution import FORMATS, PLATFORMS, TOOLS, TOOLS_PLATFORMS, MACOS_DEPLOYMENT_TARGET, sha256

GLIBC_LIBRARIES = {"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0",
                   "libresolv.so.2", "librt.so.1"}
ELF_MACHINES = {"linux-amd64": "Advanced Micro Devices X86-64", "linux-arm64": "AArch64"}
INTERPRETERS = {"linux-amd64": "/lib64/ld-linux-x86-64.so.2",
                "linux-arm64": "/lib/ld-linux-aarch64.so.1"}


def output(*command):
    return subprocess.check_output(command, text=True)


def pe_dependencies(path, platform):
    """Read PE architecture/imports without executing or requiring an SDK."""
    data = path.read_bytes()
    pe, = struct.unpack_from("<I", data, 0x3c)
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"invalid PE executable: {path}")
    machine, count = struct.unpack_from("<HH", data, pe + 4)
    expected = {"windows-amd64": 0x8664, "windows-arm64": 0xaa64}[platform]
    if machine != expected:
        raise ValueError(f"wrong PE architecture: {path}: {machine:#x}")
    optional_size, = struct.unpack_from("<H", data, pe + 20)
    optional = pe + 24
    if struct.unpack_from("<H", data, optional)[0] != 0x20b:
        raise ValueError(f"expected PE32+ executable: {path}")
    sections = []
    for index in range(count):
        entry = optional + optional_size + index * 40
        size, address, raw_size, raw = struct.unpack_from("<IIII", data, entry + 8)
        sections.append((address, max(size, raw_size), raw))

    def offset(rva):
        for address, size, raw in sections:
            if address <= rva < address + size:
                return raw + rva - address
        raise ValueError(f"unmapped PE RVA {rva:#x}: {path}")

    import_rva, = struct.unpack_from("<I", data, optional + 120)
    libraries = []
    cursor = offset(import_rva)
    while any(data[cursor:cursor + 20]):
        name_rva, = struct.unpack_from("<I", data, cursor + 12)
        start = offset(name_rva)
        name = data[start:data.index(b"\0", start)].decode("ascii").lower()
        if name not in {"kernel32.dll", "ntdll.dll", "ucrtbase.dll"} and not name.startswith("api-ms-win-crt-"):
            raise ValueError(f"unexpected PE dependency: {path}: {name}")
        libraries.append(name)
        cursor += 20
    delay_rva, = struct.unpack_from("<I", data, optional + 112 + 13 * 8)
    if delay_rva:
        raise ValueError(f"unexpected delay imports: {path}")
    return {"machine": hex(machine), "libraries": libraries}


def audit(root, platform):
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            if path.readlink().is_absolute() or not path.resolve().is_relative_to(root):
                raise ValueError(f"non-relocatable symlink: {path}")
            if not path.exists():
                raise ValueError(f"dangling symlink: {path}")
            continue
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        relative = path.relative_to(root).as_posix()
        if magic[:2] == b"MZ" and platform.startswith("windows-"):
            records.append({"path": relative, **pe_dependencies(path, platform)})
        elif magic == b"\x7fELF":
            if platform not in ELF_MACHINES:
                raise ValueError(f"ELF in macOS distribution: {path}")
            info = output("readelf", "-h", "-l", "-d", "-V", str(path))
            machine = re.search(r"Machine:\s*(.+)", info).group(1)
            if machine != ELF_MACHINES[platform]:
                raise ValueError(f"wrong ELF machine: {path}: {machine}")
            versions = sorted(set(re.findall(r"Name: GLIBC_([0-9.]+)", info)))
            if any(tuple(map(int, version.split("."))) > (2, 17) for version in versions):
                raise ValueError(f"glibc newer than 2.17: {path}: {versions}")
            libraries = re.findall(r"Shared library: \[([^]]+)\]", info)
            for library in libraries:
                # GCC's ARM64 support code can reference the glibc loader
                # directly. It is already required by this executable's ABI.
                if library not in GLIBC_LIBRARIES and library not in (
                    "ntdll.so", "win32u.so", Path(INTERPRETERS[platform]).name
                ):
                    raise ValueError(f"unexpected ELF dependency: {path}: {library}")
            interpreters = re.findall(r"Requesting program interpreter: ([^]]+)", info)
            if interpreters and interpreters != [INTERPRETERS[platform]]:
                raise ValueError(f"unexpected ELF interpreter: {path}: {interpreters}")
            for value in re.findall(r"\((?:RPATH|RUNPATH)\).*?\[([^]]+)\]", info):
                for entry in value.split(":"):
                    if not entry.startswith("$ORIGIN/") or not (
                        path.parent / entry.removeprefix("$ORIGIN/")
                    ).resolve().is_relative_to(root):
                        raise ValueError(f"non-relocatable ELF search path: {path}: {value}")
            records.append({"path": relative, "machine": machine, "glibc": versions,
                            "libraries": libraries, "interpreter": interpreters})
        elif magic in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
            if platform != "macos-arm64" or output("lipo", "-archs", str(path)).strip() != "arm64":
                raise ValueError(f"unexpected Mach-O architecture: {path}")
            libraries = [line.strip().split(" (", 1)[0]
                         for line in output("otool", "-L", str(path)).splitlines()[1:]]
            identifiers = output("otool", "-D", str(path)).splitlines()[1:]
            libraries = [library for library in libraries if library not in identifiers]
            commands = output("otool", "-l", str(path))
            minima = re.findall(r"\bminos ([0-9.]+)", commands)
            if not minima or any(tuple(map(int, item.split("."))) >
                                 tuple(map(int, MACOS_DEPLOYMENT_TARGET.split("."))) for item in minima):
                raise ValueError(f"Mach-O exceeds declared deployment target: {path}: {minima}")
            rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset", commands)
            search = []
            for entry in rpaths:
                if not entry.startswith("@loader_path/"):
                    raise ValueError(f"non-relative Mach-O search path: {path}: {entry}")
                directory = (path.parent / entry.removeprefix("@loader_path/")).resolve()
                if not directory.is_relative_to(root):
                    raise ValueError(f"Mach-O search path outside archive: {path}: {entry}")
                search.append(directory)
            for library in libraries:
                if library.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if library.startswith("@rpath/") and any(
                    (directory / library.removeprefix("@rpath/")).is_file() for directory in search
                ):
                    continue
                raise ValueError(f"unexpected Mach-O dependency: {path}: {library}")
            records.append({"path": relative, "machine": "arm64", "libraries": libraries,
                            "deployment_targets": minima})
    if not records:
        raise ValueError("No native executables found")
    return records


def verify_archive_pair(gzip_path):
    zstd_path = gzip_path.with_name(gzip_path.name.removesuffix(".tar.gz") + ".tar.zst")
    with gzip.open(gzip_path, "rb") as stream:
        expected = hashlib.file_digest(stream, "sha256").hexdigest()
    if os.name == "nt":
        from compression import zstd
        with zstd.open(zstd_path, "rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError(f"compression formats contain different tar streams: {gzip_path}")
        return
    with subprocess.Popen(["zstd", "-dc", str(zstd_path)], stdout=subprocess.PIPE) as process:
        actual = hashlib.file_digest(process.stdout, "sha256").hexdigest()
        process.stdout.close()
        if process.wait() != 0 or actual != expected:
            raise ValueError(f"compression formats contain different tar streams: {gzip_path}")


def check_artifact(artifact, platform):
    name = artifact.name.removesuffix(".tar.gz")
    hashes = {}
    for kind in ("", "-sources"):
        verify_archive_pair(artifact.parent / (name + kind + ".tar.gz"))
        for extension in FORMATS:
            path = artifact.parent / (name + kind + extension)
            digest = sha256(path)
            if (path.parent / (path.name + ".sha256")).read_text() != f"{digest}  {path.name}\n":
                raise ValueError(f"checksum mismatch: {path.name}")
            hashes[path.name] = digest
    with tempfile.TemporaryDirectory(prefix="wine artifact ") as temporary:
        root = Path(temporary).resolve()
        with tarfile.open(artifact) as archive:
            archive.extractall(root, filter="data")
        manifest = json.loads((root / "manifest.json").read_text())
        product = manifest.get("product", "runtime")
        suffix = ".exe" if platform.startswith("windows-") else ""
        executables = ["bin/" + tool + suffix for tool in TOOLS] if product == "tools" else ["bin/wine", "bin/wineserver"]
        for relative in executables:
            path = root / relative
            if not path.is_file() or not os.access(path, os.X_OK):
                raise ValueError(f"missing executable: {relative}")
        if manifest != json.loads((artifact.parent / f"{name}.manifest.json").read_text()):
            raise ValueError("embedded and external manifests differ")
        requirements = TOOLS_PLATFORMS[platform] if product == "tools" else PLATFORMS[platform]["requirements"]
        if manifest["platform"] != platform or manifest["requirements"] != requirements:
            raise ValueError("manifest does not match declared executor/guest requirements")
        if product == "runtime" and manifest["windows_cpus"] != PLATFORMS[platform]["guests"]:
            raise ValueError("manifest guest CPUs differ")
        required_directories = ["lib/wine", "LICENSES/wine"]
        if product == "runtime" or platform.startswith("windows-"):
            required_directories.append("LICENSES/llvm-mingw")
        for required in required_directories:
            if not (root / required).is_dir():
                raise ValueError(f"missing distribution directory: {required}")
        if product == "tools":
            required_files = ["include/wine/windows/unknwn.idl", "include/wine/windows/winresrc.h",
                              "lib/wine/x86_64-windows/stdole2.tlb", "lib/wine/aarch64-windows/stdole2.tlb"]
            if not platform.startswith("windows-"):
                required_files += ["share/wine/nls/c_1252.nls", "share/wine/nls/locale.nls"]
            for relative in required_files:
                if not (root / relative).is_file():
                    raise ValueError(f"missing tool support file: {relative}")
            if manifest["target_cpus"] != ["amd64", "arm64"]:
                raise ValueError("unexpected tools target CPUs")
        elif platform.endswith("arm64"):
            for module in ("libarm64ecfex.dll", "libwow64fex.dll"):
                if not (root / "lib/wine/aarch64-windows" / module).is_file():
                    raise ValueError(f"missing required FEX module: {module}")
        records = audit(root, platform)
        if product == "tools" and {record["path"] for record in records} != set(executables):
            raise ValueError("tool executable architecture checks are incomplete or extra binaries are present")
        with tarfile.open(artifact.parent / (name + "-sources.tar.gz")) as source:
            names = set(source.getnames())
            for required in ("wine/COPYING.LIB", "recipe/sources.json", "recipe/tools/build.py", "manifest.json"):
                if required not in names:
                    raise ValueError(f"missing corresponding source: {required}")
            if product == "runtime" and platform.endswith("arm64") and "fex/CMakeLists.txt" not in names:
                raise ValueError("missing FEX source")
            if json.load(source.extractfile("manifest.json")) != manifest:
                raise ValueError("source and runtime manifests differ")
    return {"status": "passed", "archives": hashes, "platform": platform, "native_files": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Runtime .tar.gz; companions must be beside it")
    parser.add_argument("--platform", required=True, choices=TOOLS_PLATFORMS)
    args = parser.parse_args()
    report = check_artifact(args.artifact.resolve(), args.platform)
    destination = args.artifact.with_name(args.artifact.name.removesuffix(".tar.gz") + ".artifacts.json")
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
