# SPDX-License-Identifier: Apache-2.0
"""Search presets and the execution contract (A11).

``quick`` is a bounded *smoke* mode for checking an installation end to end:
one direct-rewrite candidate, one proposed rule, one e-graph iteration with
deterministic rule selection. It keeps every correctness gate and every
measurement stage. It is not the paper's exploration setting.

``paper`` reproduces the paper's execution settings: >=10 iterations, >=3
distinct numerical cost configurations, >=3 distinct rule combinations, an
agent-driven rule-subset / cost-multiplier choice each iteration and an agent
stop decision after stagnation.
"""

from __future__ import annotations

from typing import Dict

PRESETS: Dict[str, Dict] = {
    "quick": {
        "max_iterations": 1, "min_iterations": 1, "min_cost_configs": 1, "min_rule_combos": 1,
        "stall_window": 1, "direct_variants": 1, "rules_to_propose": 1, "eqsat_iterations": 4,
        "eqsat_timeout_s": 120, "agent_selection": False, "agent_stop": False,
    },
    "paper": {
        "max_iterations": 60, "min_iterations": 10, "min_cost_configs": 3, "min_rule_combos": 3,
        "stall_window": 5, "direct_variants": 3, "rules_to_propose": 2, "eqsat_iterations": None,
        "eqsat_timeout_s": 600, "agent_selection": True, "agent_stop": True,
    },
}


def resolve(search_cfg: Dict) -> Dict:
    cfg = dict(search_cfg or {})
    preset = cfg.get("preset", "quick")
    out = dict(PRESETS[preset])
    out["preset"] = preset
    for k, v in cfg.items():
        if k != "preset" and v is not None:
            out[k] = v
    if out["min_iterations"] > out["max_iterations"]:
        raise ValueError("search.min_iterations exceeds search.max_iterations")
    out.setdefault("allow_latency_change", False)
    return out


def contract_status(settings: Dict, successful_iterations: int, cost_configs: int, rule_combos: int,
                    attempted: int) -> Dict:
    problems = []
    if successful_iterations < settings["min_iterations"]:
        problems.append(f"{successful_iterations}/{settings['min_iterations']} successful iterations")
    if cost_configs < settings["min_cost_configs"]:
        problems.append(f"{cost_configs}/{settings['min_cost_configs']} distinct numerical cost configurations")
    if rule_combos < settings["min_rule_combos"]:
        problems.append(f"{rule_combos}/{settings['min_rule_combos']} distinct rule combinations")
    return {
        "preset": settings["preset"],
        "status": "met" if not problems else "incomplete",
        "problems": problems,
        "attempted_iterations": attempted,
        "successful_iterations": successful_iterations,
        "distinct_cost_configs": cost_configs,
        "distinct_rule_combos": rule_combos,
    }
