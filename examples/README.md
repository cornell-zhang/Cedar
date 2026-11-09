# Examples

* `dist_mac/` — a registered datapath computing `a*b + a*c` (8-bit inputs, 16-bit
  result) with a known optimization: factoring into `a*(b+c)` removes a
  multiplier. Contains the RTL, an activity-only testbench (`tb/tb_activity.v`,
  power analysis), a self-checking testbench (`tb/tb_check.v`, for
  `verification.mode: simulation`) and a design spec (`design.yaml`).
  It is the design used for the release's fast full-flow test.
* `config/` — placeholder tool and technology profiles. Copy them to your private
  config home and adapt them, or create them with the `cedar-setup` skill.
* The open-source compiler example (`cedar example open`) ships inside the
  `cedar-workflow` package (`src/cedar/data/examples/dot3.v`).

Quick full-PPA run (requires your EDA tools and profiles):

```bash
cedar run start --design examples/dist_mac/design.yaml --work-dir /scratch/cedar-runs \
    --profile my-site --technology mypdk-tt --agent <claude|codex> --model <model>
```
