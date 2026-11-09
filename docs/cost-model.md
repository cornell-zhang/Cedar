# Objectives, per-e-node costs and physical attribution

Cedar keeps three related but distinct mechanisms apart.

## 1. Design-level weighted objective (ranking, prompts, stopping)

`src/cedar/objective.py`:

    score = w_area * A / A_ref + w_delay * D / D_ref + w_power * P / P_ref     (lower is better)

* `A` = DC post-synthesis total cell area (um2), `D` = DC post-synthesis
  critical-path arrival time (ns), `P` = PrimeTime post-route total power with
  SAIF activity and SPEF parasitics (uW).
* `normalization: baseline` divides by the baseline's value of the same metric
  at the same stage. If a needed reference is missing or non-positive, or a
  candidate metric is unavailable, the score is *undefined* (reported), never
  computed from a substituted zero.
* Timing is a hard constraint (`require_timing_met`), separate from the soft
  delay weight: infeasible candidates never rank above feasible ones.
* `weight_schedule` switches weights from a given iteration on.
* The resolved objective text is inserted into every agent prompt.

This score ranks *measured whole designs*. It is not a per-e-node attribution.

## 2. Per-e-node weighted cost model (extraction)

`src/cedar/egraph/costmodel.py` implements the paper's node cost

    cost(n) = alpha * area(n)/S_area + beta * delay(n)/S_delay + gamma * power(n)/S_power

with `(alpha, beta, gamma)` = the objective weights of the current iteration.

Node features are technology-independent **estimates** (gate-equivalent scale)
from operator kind, result width `w`, operand count `k`:

| Operator | area(n) | delay(n) | notes |
| --- | --- | --- | --- |
| `comb_add`, `comb_sub` | 2 w (k-1) | 2 log2 w * ceil(log2 k) | |
| `comb_mul` | w^2 (k-1) | 3 log2 w (k-1) | constant operands are not specialized |
| `comb_and`, `comb_or` | 0.5 w (k-1) | ceil(log2 k) | |
| `comb_xor` | w (k-1) | 1.5 ceil(log2 k) | |
| `comb_mux` | w | 1 | |
| `comb_icmp` | 1.5 w_op | 1.5 log2 w_op | operand width |
| shifts | w log2 w (0 if the amount is constant) | log2 w (0 if constant) | |
| `seq_firreg` | 4 w | 0 | power adds a clock term w |
| concat/extract/replicate/constants/values | 0 | 0 | wiring |
| other / opaque | 1 | 1 | unknown |

`power(n) = area(n) * toggle(op)` (mul 1.5, xor 1.2, add/sub 1.1, else 1.0) — a
switched-capacitance proxy that ignores input activity.

Scales `S_*` are the summed features of the unoptimized design's e-graph
(analysis run without rules), so weights act on comparable magnitudes. Costs
are written into the serialized e-graph's `cost` fields and consumed by
`extract_result` (DAG-sharing greedy extraction). Because the extractor sums
node costs, the delay term approximates the total logic depth of the selected
DAG, **not** the critical path; measured delay is always taken from timing
reports. `tests/test_costmodel.py::test_extractor_choice_flips_with_weights`
checks with the real extractor that an area weighting and a delay weighting
choose different implementations.

Two optional multiplicative adjustments refine features per constructor:
agent-proposed `cost_multipliers` (paper preset, range [0.05, 20]) and the
calibration table from physical attribution (below).

## 3. Netlist-to-e-node attribution (A2)

`src/cedar/attribution.py`, run after each measured extracted candidate:

1. **gate -> RTL line/operator**: DC synthetic-operator instance names
   (`mult_57_*`, `add_57_*`) give operator and line; other cells use DC
   `report_cross_probing` (with `hdlin_enable_rtlinfo`); flip-flops map to
   registers.
2. **RTL line -> e-nodes**: the candidate RTL is ExportVerilog output of the
   extracted term, so each operator token on a line corresponds to one
   extracted e-node of that kind.
3. **measurements**: per-cell area (DC reference unit areas), per-cell
   post-route power (PrimeTime `report_power -cell_power` with SAIF), and the
   DC critical path.

A cell shared by several e-nodes is split equally among them, so each gate's
area and power is counted exactly once. P&R-inserted cells and cells without
location information are left unattributed and reduce the reported
**coverage** (area, power, critical-path delay). Per-operator calibration
factors `(measured share)/(estimated share)` (clamped to [0.1, 10]) feed the
next iteration's node features; operators on the critical path receive a delay
calibration. The equal split and token-level identification are heuristics:
the result is a calibrated estimate, not a measured per-e-node power.
