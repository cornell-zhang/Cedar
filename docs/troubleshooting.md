# Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `no Cedar toolchain selected` | Install the `cedar-toolchain` wheel, or set `CEDAR_TOOLCHAIN_PREFIX` / `--toolchain-prefix` / `toolchain.prefix`. Cedar deliberately ignores tools on `PATH`. |
| `toolchain ... is incomplete; missing: ...` | The prefix lacks `bin/<tool>` or `share/cedar/*.egg`; rebuild/reinstall. |
| `manifest_match: false` in `cedar doctor` | Binaries differ from the build manifest (partial rebuild or manual copy). Reinstall the wheel or rebuild the prefix. |
| run status `missing_capability` | A stage required by the measurement mode or the verification backend is not configured/available. Run `cedar doctor --profile ... --technology ...`. |
| `required environment variable X is not set` (exit 98) | Your profile lists `X` in `env_required`; export it in the setup script. |
| `setup script failed` (exit 97) | The profile's `environment.setup_script` returned non-zero; run it manually in bash. |
| run status `infeasible_timing_goal` | The original design misses the declared clock. Choose a feasible period (Cedar never relaxes it) or set `objective.require_timing_met: false` to explore anyway. |
| baseline `power: failed (SAIF annotated only N% of nets)` | `activity.tb_top` / `dut_instance` do not match the testbench hierarchy, so PrimeTime used default activity. |
| DC `unmapped cells after compile` | The library lacks cells for a construct (e.g. tri-states, latches); adjust the RTL or `synthesis.dont_use`. |
| Conformal `incomplete verification items` / unmapped points | Key points could not be mapped (e.g. renamed or re-encoded registers). The candidate is excluded; add site-specific mapping commands via `verification.conformal_extra` or `equivalence.extra_commands` if appropriate. |
| `verification backend ... unavailable` | Runs stop rather than accept unchecked candidates. Fix the tool or choose another explicit `verification.mode`. |
| `circt-lec ... combinational designs only` | Use `conformal` or a self-checking `simulation` testbench for sequential designs. |
| task REJECTED: `port list differs from the original` | Direct rewrites and reconstructions must keep the module name, port names, directions and widths. |
| task REJECTED: `expected exactly one rule form` | One `(rule ...)` with one `union` per file. |
| rule `UNSUPPORTED: operand vector(s) without one exact (= N (vec-length ?v)) guard, or read out of the guarded bounds` | Add exactly one guard with the vector's exact operand count (0 allowed) and read only literal indices below it; the proof models exactly N operands. |
| rule `REJECTED: unsupported premise shape ...` | Premises must be `(= ?var term)` or `(= N (vec-length ?v))`. To equate two terms, bind both to one shared variable. |
| rule `UNSUPPORTED: ... independent type variables` | Rules that relate different widths are not certified in v0.1; write one rule per width relation with concrete types. |
| rule `REFUTED` at some width only | The identity does not hold at that width (e.g. it holds modulo 2 but not modulo 2^8). Only proved widths are applied. |
| CIRCT extraction failed | The run creates an `extraction_fallback` task; the agent's reconstruction is verified like any candidate. Inspect `egraph/iterNN/*.mlir` and the egg-opt stderr in `run.json`. |
| Codex cannot reach the license server | Run Codex with a sandbox policy that allows network access for the EDA commands. |
| Claude Code / Codex does not show the skills | Check the plugin is installed and enabled (`claude plugin list`, `codex plugin list`) or start Claude with `--plugin-dir /path/to/Cedar`. |
