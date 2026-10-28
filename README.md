# wine-prebuilt

Source-built, headless Wine distributions for Windows build tools. One archive
per executor OS/CPU; ARM64 archives include FEX. No Rosetta, QEMU, Linux guest
rootfs, system Wine installation, or Windows SDK is required.

| Executor | Windows guest CPUs | Declared runtime requirement |
| --- | --- | --- |
| Linux amd64 | amd64, i386 | glibc 2.17+, installed UTF-8 locale |
| Linux ARM64 | ARM64, amd64, i386 | glibc 2.17+, installed UTF-8 locale |
| macOS ARM64 | ARM64, amd64 | deployment target 26.5; see compatibility below |

Windows executors run Windows programs natively and need no Wine runtime.
Bazel integration belongs in `rules_wine`.

The **same release** also contains `wine-tools` archives with native `wmc`,
`wrc`, and `widl` for Linux amd64/ARM64, macOS ARM64, and Windows amd64/ARM64.
These generate Windows messages, resources, and IDL outputs without running
Wine or FEX. Consumers download binaries; no source compiler is required to
run them. Compiling generated C remains the application's responsibility.

## Pipeline

GitHub Actions calls these operations explicitly, in order. A failed stage
prevents attestation and publication. Linux and macOS use the same commands.

| Stage | Command | Inputs → outputs |
| --- | --- | --- |
| Build | `tools/build.py PLATFORM` | Pinned sources and patches → `.work/PLATFORM/{install,sources,recipe,build.json}` |
| Build tools | `tools/build-tools.py PLATFORM` | Same pinned Wine source → `.work/tools-PLATFORM/{install,sources,recipe,build.json}` |
| Package | `tools/package.py BUILD_DIRECTORY` | Installed runtime and prepared sources → archives, manifest, checksums in `out/` |
| Check artifacts | `tests/artifacts.py ARCHIVE --platform PLATFORM` | Actual archives → `.artifacts.json` |
| Compile test fixtures | `tests/build.py BUILD_DIRECTORY` | Pinned compiler and test sources → `BUILD_DIRECTORY/fixtures/` |
| Test runtime | `tests/runtime.py ARCHIVE FIXTURES` | Relocated archive, every supported guest CPU, Windows Ninja → `.qualification.json` |
| Test compatibility | `tests/compatibility.py ARCHIVE FIXTURES` | Same Linux archive in pinned CentOS 7 userspace → `.glibc217.json` |
| Test tools | `tests/tools.py TOOLS_ARCHIVE` | Relocated native tools → resource/header/type-library checks and `.qualification.json` |
| Test tools compatibility | `tests/tools.py TOOLS_ARCHIVE --compatibility` | Same Linux tools in CentOS 7 userspace → `.glibc217.json` |
| Attest | `actions/attest` | Checked archives and reports → signed `.attestation.jsonl` |
| Publish | `tools/release.py TAG DIRECTORY` | Exact tested artifacts and attestations → draft GitHub release |

Build does no packaging or runtime testing. Package does no compilation or ABI
testing. Publication downloads the workflow artifacts; it never rebuilds them.

`tools/build.py` owns source preparation, the Linux sysroot or macOS SDK setup,
and compilation. `build-wine.sh` contains Wine's configure flags;
`build-fex.sh` builds the required ARM64EC and WoW64 modules on ARM64 executors.
FEX is a source-built dependency included in the Wine install tree.

`build-tools.py` compiles only the three developer tools, generates their
Flex/Bison parsers, and installs their support files. Windows tools are
cross-compiled with pinned LLVM-MinGW on Linux, then tested on native
`windows-2025` and `windows-11-arm` runners before attestation. Native WIDL
generates the standard target type libraries during a Windows cross-build.
No full Wine runtime is built for Windows.

Build directories have ordinary, predictable names. Use a fresh `--work`
directory for a rebuild; failed builds and test logs remain available for
inspection. Downloads are cached by verified SHA-256. There is no build cache
key derived from documentation or machine package inventories.

## Local commands

Use a native executor of the artifact CPU. Python 3.12+ is required. Run from
the repository root. For Linux:

```sh
docker build -t wine-builder .
wine_platform=linux-amd64 # linux-arm64 on a native ARM64 executor
wine_exec=tools/in-linux-container.sh
```

For macOS ARM64, with Xcode command-line tools and Homebrew:

```sh
brew install bison cmake ninja python@3.14 zstd
wine_platform=macos-arm64
wine_exec=
```

Then run the same individual operations used by CI:

```sh
$wine_exec python3 -m unittest discover -s tests -p '*_test.py'
$wine_exec python3 tests/source-preparation.py
$wine_exec python3 tools/build.py "$wine_platform" --jobs 4
$wine_exec python3 tools/package.py ".work/$wine_platform" --serial 2
wine_artifact="out/wine-$wine_platform-11.20.2.tar.gz"
$wine_exec python3 tests/artifacts.py "$wine_artifact" --platform "$wine_platform"
$wine_exec python3 tests/build.py ".work/$wine_platform"
$wine_exec python3 tests/runtime.py "$wine_artifact" ".work/$wine_platform/fixtures"
```

On Linux, run the compatibility test from the host; it starts its own pinned
runtime container without mounting the build/install tree:

```sh
python3 tests/compatibility.py "$wine_artifact" ".work/$wine_platform/fixtures"
```

Build and test the tools independently (same Linux container wrapper or native
macOS environment as above):

```sh
$wine_exec python3 tools/build-tools.py "$wine_platform"
$wine_exec python3 tools/package.py ".work/tools-$wine_platform" --serial 2
tools_artifact="out/wine-tools-$wine_platform-11.20.2.tar.gz"
$wine_exec python3 tests/artifacts.py "$tools_artifact" --platform "$wine_platform"
$wine_exec python3 tests/tools.py "$tools_artifact"
# Linux host only:
python3 tests/tools.py "$tools_artifact" --compatibility
```

For Windows, run `tools/build-tools.py windows-amd64` or `windows-arm64` on
a supported Unix builder, then package its `.work/tools-PLATFORM` directory.
Extract the workflow artifacts on the corresponding native Windows worker and
run `python tests/artifacts.py ARCHIVE --platform PLATFORM` followed by
`python tests/tools.py ARCHIVE`. Windows artifact checks require Python 3.14
for its standard-library zstd reader. Tool functional tests need no compiler,
Wine installation, or SDK.

The Linux Docker image supplies build tools. `in-linux-container.sh` only runs
the requested operation with the invoking user's UID/GID and mounted checkout.
Compilation happens during `docker run`, rather than QEMU's `docker build`, so
Actions can expose packaging and tests separately and retain ordinary files
between stages. macOS uses the native runner and Apple Clang.

## Verification and compatibility

Artifact tests check both compression formats, checksums, root layout,
corresponding sources, executable permissions, native architecture, allowed
libraries, interpreter paths, and relative symlinks/loader search paths.
Linux GLIBC symbol versions cannot exceed 2.17. Every Mach-O deployment minimum
must fit the declared target. Audit records belong to the artifact-test report,
not the package manifest.

Runtime tests extract the archive elsewhere, including paths with spaces, and
use fresh prefixes. They exercise all advertised guest CPUs, cross-CPU child
processes, DLL loading, threads, Unicode/empty arguments, filesystem output,
exit codes, and upstream Windows Ninja. The compatibility test repeats the
fixtures in CentOS 7 userspace; it does not establish an old Linux kernel floor.

The Linux build sysroot comes from pinned CentOS 7 RPMs. amd64 uses the matching
static GCC unwinder to avoid a newer glibc dependency. Wine dynamically links
the executor's glibc and its own Unix modules; it is not fully static.

**macOS 26.5 is an imposed deployment target, not a demonstrated intrinsic
Wine/FEX requirement.** Build metadata records that target and the actual Xcode
SDK version separately. Runtime reports record the OS used for testing. CI's
`macos-26` image can be newer than 26.5; passing there does not prove operation
on 26.5 or any earlier release. The loader patch's 11.0 default does not qualify
the complete runtime. Lowering the target requires a separate rebuild and
compatibility test effort.

## Updating and releasing

The initial recipe pins Wine **11.20** (a development release), LLVM-MinGW
**20261020**, and MetalSharp's FEX fork. `sources.json` contains exact revisions,
URLs and checksums, including FEX dependencies and test inputs. Both Linux
builds patch Wine's address-space probe for the older sysroot headers;
Linux ARM64 also adds FEX registration. macOS applies its explicit patch series.
See [patch provenance](patches/PROVENANCE.md) for each patch's purpose and origin.

Update those pins and checksums, review/rebase patches, and run the three native
runtime executors and all five tools executors. The tools source lists follow
Wine's `tools/{wmc,wrc,widl}/Makefile.in`; review them when changing Wine versions.
Windows tools apply `patches/tools-mingw-fastfail.patch` to avoid defining an
intrinsic already provided by MinGW's headers. The tools do not need the macOS
Wine/FEX runtime patches. Commit the recipe before a release build. Each build preserves its
recipe files and records the Git commit, dirty-checkout status, source pins,
compiler and relevant SDK information. Release publication rejects dirty builds.
Host build packages can change, so byte-identical rebuilds are not claimed.

Tags use `<wine-version>.<artifact-serial>`. Increment the serial for a recipe
change without a Wine version change. Arbitrary version overrides and historical
backfills are not supported.

```sh
git tag 11.20.2
git push origin 11.20.2
# Retry a release workflow at the existing tag:
gh workflow run release.yml --ref 11.20.2 -f tag_name=11.20.2
```

CI and release workflows share `reusable-build.yml`. The release workflow
validates the tag, runs the stages, and passes their artifacts to a publication
job. Public releases require the signed provenance bundle for each executor.
Pull requests run checks without attestation.

For an upload failure, use **Re-run failed jobs** to retain the original build
artifacts. Existing assets are accepted only if their bytes match. Conflicting
assets require a new serial. Releases are assembled as **drafts**; publishing
is a separate explicit decision. macOS artifacts remain unsigned/not notarized.

## Consumer contract

Names are `wine-<os>-<cpu>-<wine-version>.<serial>.tar.gz` and `.tar.zst`.
Both contain the same normalized tar bytes and have `.sha256` companions.
Source archives insert `-sources` before `.tar`, containing `wine/`, optional
`fex/`, the exact `recipe/`, and `manifest.json`.

Extract the complete runtime archive. `bin/wine`, `bin/wineserver`, `lib/wine/`,
notices and `manifest.json` are at its root; no `strip_prefix` is needed.
Schema version 1 records executor OS/CPU, Windows guest CPUs, requirements,
relative executable paths, Wine version, artifact serial and source/build
provenance. The manifest is also a separate release asset.

Tools use `wine-tools-<os>-<cpu>-<wine-version>.<serial>` with the same formats,
source companions, checksums and attestation conventions. Their manifest has
`product: "tools"`, executable paths, `include_dir`, `typelib_dir`, `nls_dir`,
and supported `target_cpus` (amd64 and ARM64). It has no Wine guest-CPU promise.
Runtime and tools archives share the source pin and release tag.

Tools archives contain:

- `bin/{wmc,wrc,widl}` (`.exe` on Windows).
- `include/wine/windows/`: source headers and IDL definitions for generation;
  this is not a complete C/C++ SDK and does not replace the application's SDK.
- `lib/wine/{x86_64,aarch64}-windows/stdole2.tlb`: standard OLE type libraries.
- `share/wine/nls/*.nls` on Unix. Windows tools use Windows' locale/codepage APIs.

Executable-relative lookup supports relocating the complete archive. For
hermetic build actions, use explicit `--nls-dir`, `--nostdinc`, `-I` and `-L`
where applicable, and declare project headers, imports and resource assets as
inputs. WIDL chooses target-specific type-library directories from its `-b`
target. PO/POT output requiring gettext is disabled. Additional type libraries
and application SDKs are supplied by consumers, not discovered from a registry.

Tools retain glibc 2.17 and the imposed macOS 26.5 deployment target. Windows
amd64 declares Windows 10/UCRT, ARM64 Windows 11/UCRT; hosted CI tests Server
2025 and Windows 11 respectively, not every declared minimum OS. Locale results
can differ between Unix's bundled tables and Windows' system tables; the
qualification report describes the tested inputs, not universal bit identity.

```sh
export WINEPREFIX="$PWD/prefix"
export WINESERVER="$PWD/wine/bin/wineserver"
"$PWD/wine/bin/wine" path/to/tool.exe
"$WINESERVER" -w
```

Verify provenance with the matching bundle:

```sh
gh attestation verify wine-linux-arm64-11.20.2.tar.zst \
  --repo hermeticbuild/wine-prebuilt \
  --bundle wine-linux-arm64-11.20.2.attestation.jsonl \
  --signer-workflow hermeticbuild/wine-prebuilt/.github/workflows/reusable-build.yml
```

Ship the matching source archives and notices with the runtime; see
[distribution requirements](DISTRIBUTION.md).

## Limitations

- Headless profile: GUI, audio, printing, multimedia, input devices and other
  optional integrations are disabled. GnuTLS/Kerberos integrations are also disabled.
- No macOS amd64 build, Rosetta fallback, ARM32 guest, or qualified macOS i386
  support. ARM64/x64 success does not establish that arbitrary Windows programs
  work. macOS FEX integration remains young.
- macOS archives are unsigned and not notarized. Passing on a build machine
  does not establish Gatekeeper acceptance of a quarantined download.
- Wine is not a security sandbox; fresh prefixes isolate mutable state, not
  filesystem or network access.
