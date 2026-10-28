#!/usr/bin/env python3
"""Verify a complete release set and upload immutable assets to a draft release."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from distribution import FORMATS, PLATFORMS, TOOLS, distributions, basename, sha256, version, serial_from_tag
from fetch import ROOT


def validate_assets(directory, wine, serial, commit):
    expected = set()
    for product, platform in distributions():
        name = basename(platform, wine, serial, product)
        archive_hashes = {}
        for kind in ("", "-sources"):
            for extension in FORMATS:
                filename = name + kind + extension
                path = directory / filename
                digest = sha256(path)
                if (directory / (filename + ".sha256")).read_text() != f"{digest}  {filename}\n":
                    raise ValueError(f"checksum mismatch: {filename}")
                expected.update((filename, filename + ".sha256"))
                if not kind:
                    archive_hashes[filename] = digest
        manifest = json.loads((directory / f"{name}.manifest.json").read_text())
        if (manifest.get("product", "runtime") != product or manifest["platform"] != platform or manifest["wine"] != wine or
                manifest["artifact_version"] != version(wine, serial) or
                manifest["build"]["recipe_commit"] != commit or manifest["build"]["recipe_dirty"]):
            raise ValueError(f"manifest does not match release recipe: {name}")
        qualification = json.loads((directory / f"{name}.qualification.json").read_text())
        if qualification["status"] != "passed" or qualification["archives"] != archive_hashes:
            raise ValueError(f"archives are not qualified: {name}")
        if product == "runtime":
            if qualification["guests"] != PLATFORMS[platform]["guests"]:
                raise ValueError(f"guest CPUs are not qualified: {name}")
        elif qualification["tools"] != list(TOOLS) or qualification["target_cpus"] != ["amd64", "arm64"]:
            raise ValueError(f"developer tools are not qualified: {name}")
        checks = json.loads((directory / f"{name}.artifacts.json").read_text())
        all_hashes = {name + kind + extension: sha256(directory / (name + kind + extension))
                      for kind in ("", "-sources") for extension in FORMATS}
        if checks["status"] != "passed" or checks["archives"] != all_hashes:
            raise ValueError(f"artifact checks do not match release bytes: {name}")
        expected.update((f"{name}.manifest.json", f"{name}.artifacts.json",
                         f"{name}.qualification.json", f"{name}.attestation.jsonl"))
        if not (directory / f"{name}.attestation.jsonl").stat().st_size:
            raise ValueError(f"empty attestation bundle: {name}")
        if platform.startswith("linux-"):
            filename = f"{name}.tar.gz.glibc217.json"
            baseline = json.loads((directory / filename).read_text())
            if (baseline["status"] != "passed" or baseline["artifact"] != f"{name}.tar.gz" or
                    baseline["sha256"] != archive_hashes[f"{name}.tar.gz"]):
                raise ValueError(f"glibc baseline is not qualified: {name}")
            expected.add(filename)
    actual = {path.name for path in directory.iterdir()}
    if actual != expected:
        raise ValueError(f"unexpected/missing release assets: {sorted(actual ^ expected)}")
    return [directory / name for name in sorted(expected)]


def gh(*arguments):
    return subprocess.check_output(["gh", *map(str, arguments)], text=True)


def compare_existing(path, digest):
    if digest != "sha256:" + sha256(path):
        raise ValueError(f"refusing to replace conflicting release asset: {path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("directory", type=Path, nargs="?", help="Previously built, tested and attested assets")
    parser.add_argument("--check-tag", action="store_true", help="Validate tag before building; emit its serial")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    wine = json.loads((ROOT / "sources.json").read_text())["wine"]["version"]
    serial = serial_from_tag(args.tag, wine)
    revision = gh("api", f"repos/{args.repository}/commits/{args.tag}", "--jq", ".sha").strip()
    if revision != args.commit:
        parser.error("release tag does not point to the build commit")
    if args.check_tag:
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(f"serial={serial}\ntag={args.tag}\n")
        print(serial)
        return
    if args.directory is None:
        parser.error("publication requires the directory of tested artifacts")
    assets = validate_assets(args.directory, wine, serial, args.commit)
    for product, platform in distributions():
        name = basename(platform, wine, serial, product)
        bundle = args.directory / f"{name}.attestation.jsonl"
        for asset in assets:
            if asset.name.startswith(name + ".") or asset.name.startswith(name + "-sources."):
                if asset != bundle:
                    gh("attestation", "verify", asset, "--bundle", bundle, "--repo", args.repository,
                       "--signer-workflow", f"{args.repository}/.github/workflows/reusable-build.yml",
                       "--source-digest", args.commit)
    # Creation may fail because a previous attempt already created the draft.
    # Reading it below must succeed before uploads can begin.
    subprocess.run(["gh", "release", "create", args.tag, "--repo", args.repository, "--verify-tag",
                    "--draft", "--title", f"Wine {args.tag}", "--notes-file",
                    str(ROOT / ".github/release-notes.md")], check=False)
    for asset in assets:
        for attempt in range(4):
            release = json.loads(gh("release", "view", args.tag, "--repo", args.repository,
                                    "--json", "assets,isDraft"))
            existing = next((item for item in release["assets"] if item["name"] == asset.name), None)
            if existing:
                # Downloading also supports older releases without API digest metadata.
                with tempfile.TemporaryDirectory() as temporary:
                    gh("release", "download", args.tag, "--repo", args.repository,
                       "--pattern", asset.name, "--dir", temporary)
                    compare_existing(asset, "sha256:" + sha256(Path(temporary) / asset.name))
                break
            if not release["isDraft"]:
                raise ValueError("refusing to add assets to an already published release")
            result = subprocess.run(["gh", "release", "upload", args.tag, str(asset),
                                     "--repo", args.repository], check=False)
            if result.returncode == 0:
                break
            if attempt == 3:
                raise RuntimeError(f"upload failed: {asset}")
            time.sleep(5 * (attempt + 1))
    print(f"Complete draft release: {args.repository} {args.tag}")


if __name__ == "__main__":
    main()
