# Private runtime artifacts

The fallback downloads CPython 3.13.15 from Astral's python-build-standalone release [20260901](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901). The six SHA256 digests in `scripts/runtime-assets.tsv` were checked against the official GitHub release API asset metadata on 2026-09-12. The launchers accept only those pinned HTTPS release URLs and verify the digest before extracting or executing an archive. Updating the pin is a reviewed package change; runtime startup does not discover a new Python release.

The selected archives are the standard `install_only_stripped` builds for macOS ARM64/x86-64, Windows ARM64/x86-64, and glibc Linux aarch64/x86-64. See upstream [distribution documentation](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst) for platform requirements. Consumers keep the archive's full Python installation, including its license and third-party notices. CPython and its bundled libraries retain their upstream licenses; this repository's MIT license applies to the launcher code, not to those downloaded components.

The fallback uses native shell/PowerShell primitives because Python is absent at that point. Bootstrap's Python-based downloader and engine lock were evaluated but are not runtime dependencies. No bootstrap engine is imported or executed.
