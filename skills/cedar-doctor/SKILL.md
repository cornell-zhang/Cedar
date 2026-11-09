---
name: cedar-doctor
description: Diagnose a Cedar installation - toolchain selection, Z3, agent CLIs, tool/technology profiles, design specs, EDA tool availability and end-to-end validation state. Use when a Cedar command fails, before a first run, or when the user asks whether Cedar is set up correctly.
---

# Cedar doctor

1. Run `cedar doctor --json` (add `--profile`, `--technology`, `--design` when known).
2. Explain the result in three levels for every capability:
   - **configured** — present and schema-valid in the profile;
   - **available** — the executable resolves inside the profile environment;
   - **validated** — `cedar flow-check` passed for this exact profile/technology and
     tool executables (`stale` means something changed since).
3. Toolchain: Cedar uses only an explicitly selected toolchain (cedar-toolchain wheel,
   `CEDAR_TOOLCHAIN_PREFIX`, `--toolchain-prefix`, or `toolchain.prefix` in the
   profile) and never whatever happens to be on PATH. `manifest_match: false` means
   the binaries differ from the build manifest — rebuild or reinstall.
4. `cedar toolchain --smoke` (source checkout) or `cedar example open --output-dir <dir>`
   checks the compiler toolchain without commercial tools.
5. Propose concrete fixes (install the wheel, fix a profile path, set a missing
   environment variable name in the site setup script, re-run `cedar flow-check`).
   Do not edit profiles without the user's agreement.
