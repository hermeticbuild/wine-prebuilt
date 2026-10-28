#!/usr/bin/env python3
"""Exercise all three native tools from a relocated distribution, without Wine."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from distribution import FORMATS, TOOLS, sha256
from fetch import ROOT


def resources(path):
    """Read numeric resource IDs from the Windows .res container."""
    data = path.read_bytes()
    result = {}
    offset = 0
    while offset < len(data):
        size, header = struct.unpack_from("<II", data, offset)
        type_marker, kind, name_marker, name = struct.unpack_from("<HHHH", data, offset + 8)
        if type_marker != 0xffff or name_marker != 0xffff or header < 32:
            raise AssertionError("fixture resources must have numeric type/name IDs")
        body = data[offset + header:offset + header + size]
        if len(body) != size:
            raise AssertionError("truncated resource")
        if size:
            result[kind, name] = body
        offset = (offset + header + size + 3) & ~3
    return result


def exercise(root, output, container=None):
    output.mkdir(parents=True)
    for path in (ROOT / "tests/tools-data").iterdir():
        shutil.copy2(path, output / path.name)
    manifest = json.loads((root / "manifest.json").read_text())

    def run(tool, *args, success=True):
        command = [str(root / manifest["executables"][tool]), *args]
        if container:
            command = ["docker", "exec", "-w", str(output), container, *command]
        process = subprocess.run(command, cwd=output, text=True, capture_output=True)
        with (output / (tool + ".log")).open("a") as log:
            log.write(json.dumps(command) + "\n" + process.stdout + process.stderr)
        if (process.returncode == 0) != success:
            raise AssertionError(f"unexpected {tool} exit {process.returncode}: {process.stderr}")

    # Default executable-relative data lookup is intentional: no build tree or
    # installed Wine is available at the relocated path.
    run("wmc", "-u", "-O", "res", "-o", "messages.res", "messages.mc")
    message = resources(output / "messages.res")[11, 1]
    blocks, first, last, position = struct.unpack_from("<IIII", message)
    assert (blocks, first, last) == (1, 1001, 1001), "wrong message IDs"
    length, flags = struct.unpack_from("<HH", message, position)
    assert flags == 1, "message must be Unicode"
    assert message[position + 4:position + length].decode("utf-16-le").rstrip("\0\r\n") == "Hello from Wine tools."
    run("wmc", "-u", "-i", "-H", "messages.h", "-o", "messages.rc", "messages.mc")
    assert "MSG_TOOLS_HELLO" in (output / "messages.h").read_text()

    run("wrc", "-u", "-o", "resources.res", "resources.rc")
    records = resources(output / "resources.res")
    assert records[10, 43] == (output / "payload.txt").read_bytes(), "resource asset differs"
    strings = records[6, 3]
    position = 0
    values = []
    for _ in range(16):
        length, = struct.unpack_from("<H", strings, position)
        position += 2
        values.append(strings[position:position + 2 * length].decode("utf-16-le"))
        position += 2 * length
    assert values[10] == "Wine tools: café", "wrong Unicode string resource"

    for cpu in ("x86_64", "aarch64"):
        run("widl", "-b", cpu + "-w64-mingw32", "-h", "-o", cpu + ".h", "interfaces.idl")
        header = (output / (cpu + ".h")).read_text()
        assert "IExampleVtbl" in header and "Ping" in header, "COM interface missing"
        run("widl", "-b", cpu + "-w64-mingw32", "-t", "-o", cpu + ".tlb", "interfaces.idl")
        typelib = (output / (cpu + ".tlb")).read_bytes()
        assert typelib[:4] == b"MSFT", "invalid type library"
        assert uuid.UUID("7cd03083-7884-4d6d-8d79-b69a94c3f94b").bytes_le in typelib
        assert b"stdole2.tlb" in typelib, "standard type library import missing"
    for tool in TOOLS:
        run(tool, "missing-input-file", success=False)
    return {"tools": list(TOOLS), "target_cpus": ["amd64", "arm64"],
            "scenarios": ["message-table", "message-header", "resource-headers-assets-unicode",
                          "idl-standard-import", "typelib-standard-import", "missing-input"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--work", type=Path, default=ROOT / ".work/tests")
    parser.add_argument("--compatibility", action="store_true", help="Execute in pinned glibc 2.17 userspace")
    args = parser.parse_args()
    artifact = args.artifact.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="wine tools relocated space-", dir=args.work.resolve()))
    root = work / "distribution"
    with tarfile.open(artifact) as archive:
        archive.extractall(root, filter="data")
    container = None
    environment = platform.platform()
    if args.compatibility:
        from compatibility import IMAGES
        executor = json.loads((root / "manifest.json").read_text())["platform"]
        environment = IMAGES[executor]
        container = subprocess.check_output([
            "docker", "run", "--rm", "-d", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{work}:{work}", environment, "sleep", "infinity"], text=True).strip()
    try:
        tests = exercise(root, work / "inputs and outputs", container)
    finally:
        if container:
            subprocess.run(["docker", "stop", "--time", "1", container], check=True)
    name = artifact.name.removesuffix(".tar.gz")
    if args.compatibility:
        report = {"status": "passed", "artifact": artifact.name, "sha256": sha256(artifact),
                  "image": environment, "scope": "glibc 2.17 userspace on executor kernel", "tests": tests}
        destination = artifact.with_name(artifact.name + ".glibc217.json")
    else:
        report = {"status": "passed", "product": "tools", "environment": environment,
                  "archives": {name + extension: sha256(artifact.parent / (name + extension)) for extension in FORMATS},
                  **tests}
        destination = artifact.with_name(name + ".qualification.json")
    destination.write_text(json.dumps(report, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
