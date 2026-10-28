# Wine patch provenance

Base: Wine 11.20, commit `7298a818b79446bae153b8d5aa27235ab3703bf9`.
Source: [MetalSharp Wine](https://github.com/metalsharp/MetalSharp-Wine),
examined fork revision `f73af7a723116d80b94d852cf58dde2587c0b55f`.
Each patch retains its originating commit, author and subject. File numbering
retains the original series positions; gaps are intentional.

The macOS subset is patches 1–19 excluding 12 and 14: 17 patches covering native
ARM64 configuration, x18/shared-user-data placement, ARM64EC image alignment,
FEX registration, low-address reservations, 16K/4K page handling, W^X/translated
memory writes, instruction-cache management, exception state, and entitlement
detection. Patch 9 suppresses startup `.local` multicast DNS waits.

Wine's macOS host integration has ABI constraints that are absent on Linux.
The patches retain optional 4K/entitlement support even though i386 is not
qualified. Their presence is not a claim that restricted entitlements are
available to an unsigned download.

Linux ARM64 applies `linux-arm64-fex.patch`, containing only the
`loader/wine.inf.in` hunk of patch 4: it registers
the ARM64EC and WoW64 FEX modules. No Darwin changes are applied to Linux.
Both Linux builds apply `linux-address-space-probe.patch`, restoring the direct
`mmap` probe from [Wine 10.0's `dlls/ntdll/unix/virtual.c`](https://github.com/wine-mirror/wine/blob/wine-10.0/dlls/ntdll/unix/virtual.c).
Wine 11.20's replacement
helper reports a relocated mapping as `EEXIST`; with the CentOS 7 headers, that
misidentifies out-of-range addresses and requests a 32 GiB protection table.
The patch preserves the older-header fallback without requiring a newer libc
or kernel. It was reproduced with the unpatched archive under a 16 GiB data
limit; the same packaged-runtime test is repeated after the fix.

Preparation uses the standard `patch` tool with fuzz disabled. `git apply`
can silently skip paths when an extracted source tree sits inside an ignored
directory of another Git repository. The source-preparation regression exercises
that layout with the real pinned Wine source before CI builds begin.

Steam bridges, bundled fonts, game assets, graphics integration and later
gaming-specific changes from the full fork are excluded. FEX uses the exact
separate fork revision in `sources.json`; do not substitute the current upstream
branch or mix in its newer Unix helper without requalifying the integration.

Porting evidence: [FEX ARM64EC build instructions](https://wiki.fex-emu.com/index.php/Development:ARM64EC)
and MetalSharp's v2.0 source/build scripts. This repository uses the shipped
LLVM-MinGW 20261020 ARM64EC runtime, with LTO disabled and a generic tuning CPU.
Native ARM64/x64 Windows fixture runs are required to validate that choice.

Standalone Windows developer tools apply only `tools-mingw-fastfail.patch`,
a local guard around Wine's `__fastfail` definition when MinGW already provides
it. With LLVM-MinGW 20261020, the preceding GNU-inline declaration otherwise
gives Wine's definition external linkage, producing duplicate symbols when
linking multiple tool translation units. All three tools failed to link before
the guard and cross-compiled for AMD64 and ARM64 after it. Native Windows CI
then exercises the packaged tools. Unix developer tools use unpatched Wine
sources; none of the Wine/FEX runtime patches are needed by these programs.
