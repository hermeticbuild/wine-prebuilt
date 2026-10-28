# Distribution contents and release gates

Each runtime is provided as `.tar.gz` and `.tar.zst`, accompanied by matching
`-sources.tar.gz` and `-sources.tar.zst` archives and SHA-256 files. The source archive includes the exact patched Wine source, the
populated FEX source and dependencies when used, and the build recipes/patches.
Do not redistribute only a binary while dropping these companion artifacts.

The same release contains separate `wine-tools` archives for all five native
executors. They contain wmc, wrc, widl, generation headers/IDL definitions,
standard AMD64/ARM64 type libraries, and Unix NLS data. Their source companions
contain Wine and the recipe, without FEX. Windows tools use Windows' native
locale APIs and UCRT; they do not contain a Wine runtime.

Wine and its derived patches retain LGPL-2.1-or-later licensing, with additional
notices for code incorporated by Wine. FEX is MIT-licensed and has third-party
components with their own notices. LLVM-MinGW contains LLVM runtimes and MinGW
runtime code under their respective licenses. Linux native code may include
GCC runtime code under the GCC Runtime Library Exception. The repository's MIT
license applies only to its original recipes and tests.

`LICENSES/` preserves named license/copyright/notice files from the source and
compiler trees. Complete source archives also preserve per-file notices. Review
the distribution and linked components when changing dependencies: filename
collection alone is not a license inventory or proof of compliance. Windows
Ninja downloads are test inputs and are not included in runtime archives.

## Release gates

1. Verify all download checksums and retain exact source revisions and patches.
2. Audit every ELF/Mach-O and Windows tools PE for architecture, runtime dependencies and relative
   search paths in the packaged artifacts. Linux GLIBC version references must
   not exceed 2.17; Mach-O deployment minima must fit the declared target.
3. Verify the two compression formats contain identical tar bytes; test the final
   extracted archive at a different path, with fresh prefixes.
   Test all advertised guest CPUs, not merely `wine --version`.
   Tools tests must generate and inspect message resources, headers, Unicode
   resources with included assets, and IDL/type libraries with standard imports.
   Cross-built Windows tools must pass on native Windows runners before attestation.
4. Run Linux checks in both the current builder and glibc 2.17 userspace.
5. Attach the runtime, source, checksums, manifest, artifact/runtime test reports and
   attestation bundles. Verify signatures, build workflow identity and source
   commit before release upload. Existing assets must match byte-for-byte;
   conflicting assets require a new serial, never an overwrite.
6. Review macOS signing before promoting an unsigned draft to a user-facing
   release. Qualification on a build machine does not establish Gatekeeper
   acceptance of a quarantined download on another machine.

The initial macOS archive is a command-line runtime, not a signed/notarized app.
Its 26.5 deployment target is a build choice. The build SDK and tested OS version
are recorded separately; no test of an older macOS minimum is implied.
ARM64 and x64 tests require neither Rosetta nor a restricted provisioning profile.
i386 needs separate work on low-address mappings and Apple's cross-architecture
entitlement. Any later signing pipeline must sign the final layout, preserve
the signatures during packaging, notarize/staple where applicable, and test the
downloaded artifact on a separate machine. Never solve this by disabling SIP or
asking users to bypass Gatekeeper.
