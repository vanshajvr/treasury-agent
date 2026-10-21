# Benchmark: Treasury Controller vs naive baselines

2000 randomised treasury worlds (seed 1000), 6-9 obligations each, 72-hour horizon, 1-2 forecast receipts. Every policy is replayed through the same world with the same payment arithmetic.

| Policy | Critical paid on time | Critical late (silent) | Penalty score (mean, 95% CI) | Floor breached | FX + fees (USD) | Unvetted payees paid /100 | Escalations /world | Warned ahead (h) |
|---|---|---|---|---|---|---|---|---|
| Pay on due date (FIFO) | 66.8% | 1362 (1362) | 256,704 [235,948, 277,828] | 69.8% | 92 | 44.2 | 0.00 | 0.0 |
| Convert everything upfront | 64.6% | 1453 (1453) | 264,237 [243,702, 287,640] | 69.2% | 108 | 44.7 | 0.00 | 0.0 |
| Priority + floor, pay on due date | 59.5% | 1663 (1663) | 305,456 [282,302, 329,414] | 0.0% | 81 | 40.0 | 0.00 | 0.0 |
| Priority + floor, pay ASAP | 89.4% | 433 (433) | 43,357 [35,867, 51,372] | 0.0% | 81 | 41.0 | 0.00 | 0.0 |
| Agent, ignoring emails | 91.9% | 333 (24) | 33,341 [27,326, 39,610] | 0.0% | 77 | 0.0 | 0.97 | 31.4 |
| Agent (full) | 91.9% | 333 (22) | 33,339 [27,324, 39,608] | 0.0% | 77 | 0.0 | 0.99 | 32.8 |

## Agent vs the strongest naive baseline (pay ASAP), same worlds, paired

| Metric (agent minus baseline) | Mean difference | 95% CI | Agent better / tie / worse |
|---|---|---|---|
| Penalty score | -10,018.00 | [-14,203.50, -6,311.00] | 578 / 1183 / 239 |
| Critical obligations late | -0.05 | [-0.06, -0.04] | 121 / 1859 / 20 |
| FX + fees (USD) | -3.41 | [-4.34, -2.42] | 650 / 1023 / 327 |

**How to read it.** *Critical* = operations_stop and time_sensitive obligations; *silent* = late with no prior escalation to a person; *warned ahead* = mean hours between that escalation and the due time. *Penalty score* sums the policy's lateness weights over late obligations (lower is better). *Floor breached* = pooled cash fell below the reserve floor at any point. Obligations to new beneficiaries need a person's approval, so they are excluded from the lateness metrics for every policy.

**Assumptions and limits.** Forecasts are calibrated (confidence c arrives with probability c); emails are noise-free confidence updates (the LLM's real accuracy is not simulated here); FX rates are static; fees follow policy.json; the exact optimiser handles up to 16 open obligations, so worlds stay small; the agent's penalty weights are also the scoring weights, so also compare the unweighted columns. In the few worlds where the agent has more critical items late than pay-ASAP, the inspected cases are rule R5 at work: it escalates a larger critical payment it cannot fund inside the floor and holds cash for that decision, but this benchmark simulates no person answering the escalation, so held items stay unpaid. In the rest, the agent discounts forecast receipts by their confidence and so declines to commit cash that is only safe if the receipts arrive in full; the baseline spends first and happens to be right when they do arrive. The email signal adds little here because execution only ever spends settled cash; its value is an earlier warning, measured in the last column.
