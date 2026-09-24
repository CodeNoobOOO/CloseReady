# Student 4 evaluation protocol

The runner scores recorded outcomes; it does not call the LLM or simulate an application run. Record observed results from isolated synthetic cases, including the actual `run_mode`, provider, model and whether inference was live. Keep development, architecture-validation and held-out scenario IDs separate. Choose an architecture using validation only; score held-out cases once against the selected automated mode and a matched manual baseline. This repo does not yet contain completed evaluation results.

Create one JSON object per line with these required fields:

```json
{"scenario_id":"validation_wrong_period_01","split":"validation","run_mode":"single","expected_outstanding":["bank_statement"],"predicted_outstanding":["bank_statement"],"next_action_acceptable":true,"critical_false_ready":false,"unauthorised_or_duplicate_or_stale_action":false,"operator_seconds":35,"manual_interventions":0,"latency_ms":2100,"model_calls":1,"estimated_cost_usd":0.002}
```

Use the same `scenario_id` for matched mode comparisons within a split. Requirement labels in the two outstanding arrays must refer to the same case-specific requirements. `estimated_cost_usd` must use recorded token usage and a documented price assumption; use zero only when known to be zero. Put a result file outside Git if it contains non-synthetic data. Run `python scripts/evaluate.py results.jsonl --output report.json`. A null precision or recall means the denominator was zero, not perfect performance. The report groups metrics by split and mode and flags held-out sets smaller than the plan's 30-scenario target.
