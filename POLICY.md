# Treasury Controller Policy

Every threshold lives in `policy.json` (tiers, penalties, spread, SWIFT fees) or `scenario.json`
(mandate, reserve floor, obligations, forecasts). Nothing below is decided by a prompt.

## Hard rules
- **R1 Settled cash only.** Money moves only against settled balances. A forecast receipt may shape the
  plan (what to fund or defer) but a payment that depends on it is `SCHEDULE_ON_DEPOSIT` and runs
  only after the deposit settles.
- **R2 Reserve floor.** Total cash (USD equivalent) never drops below the floor after any payment,
  even with human approval.
- **R3 Idempotency.** Every mutating API call gets its own `request_id`, stored before the call.
  Retries reuse it. An FX quote is booked once. *(executor stage, not in the offline core yet)*
- **R4 Ambiguous errors.** After a timeout or unclear response, query the list endpoint for that
  `request_id` before retrying. *(executor stage)*
- **R5 Hold.** While a more critical obligation is escalated under E3, less critical items that
  could run now are `HOLD`, so cash a person may want to redirect is not spent.

## How a plan is made
1. **Optimise** (exact brute force over subsets, capped at 16 open obligations): minimise
   `penalty of deferred items`, then prefer deferring later-due items, then lowest FX + SWIFT cost.
   Planning cash = settled cash + (tier weight x expected receipt), and a receipt only helps items
   due at or after its own due time.
2. **Classify** funded items against **settled cash only**, in priority order (criticality, then due
   time). Items that fit are `EXECUTE_NOW`. Items that do not are `SCHEDULE_ON_DEPOSIT`; the part of
   their cost that settled cash cannot cover is their *forecast-reliant spend*.
3. **Gate** with the rules below.

Priority and penalty: `operations_stop` 1,000,000 | `time_sensitive` 50,000 | `important` 10,000 | `deferrable` 500.

## Forecast confidence tiers
| confidence | receipt weight when planning | max forecast-reliant spend (USD eq.) |
|------------|------------------------------|--------------------------------------|
| >= 0.8     | 100%                         | 8,000                                |
| 0.5 - 0.8  | 50%                          | 4,000                                |
| < 0.5      | 0%                           | 0                                    |

Effective confidence = amount-weighted mean over unsettled receipts (1.0 if none are forecast).

## Escalation triggers (always to a person)
- **E1** Beneficiary is new: never auto-paid, whatever the cash position. The reason states whether
  it would fit inside the floor if approved.
- **E2** Total forecast-reliant spend is above the tier limit: every scheduled item is escalated for
  approval of the *schedule* (it still only runs after settlement).
- **E3** An `operations_stop` or `time_sensitive` obligation cannot be funded. The escalation carries
  precomputed options: `wait_for_receipt`, `reprioritise`, `request_extension`.
- **E4** *(executor stage)* Quote rate differs from the planning rate by more than 0.5%: re-plan
  once, then escalate.

## Recompute rule
On a balance change, keep decisions whose funding does not touch the changed currency pool and
re-decide only the rest. A USD deposit touches every decision funded from USD (including
conversions to EUR/GBP); a GBP payout funded from the GBP wallet is locked.

## LLM boundary
May: read emails, propose a confidence value with a reason, explain decisions.
May not: set amounts, limits, floors, tiers, or approve anything.
Currently `treasury/signals.py` is a keyword stub with the same interface the LLM step will use.

## Expected plans (covered by `tests/test_planner.py`)
| confidence | freight | supplier | contractor | tooling | licence |
|------------|---------|----------|------------|---------|---------|
| 0.9 | EXECUTE_NOW | SCHEDULE_ON_DEPOSIT | SCHEDULE_ON_DEPOSIT | DEFER | ESCALATE (E1) |
| 0.6 | EXECUTE_NOW | SCHEDULE_ON_DEPOSIT | DEFER | DEFER | ESCALATE (E1) |
| 0.4 | EXECUTE_NOW | ESCALATE (E3) | HOLD (R5) | HOLD (R5) | ESCALATE (E1) |

After the receipt settles (demo): supplier and contractor `EXECUTE_NOW`, tooling `DEFER`, licence
`ESCALATE`; remaining cash about 3,268 USD against a 2,500 floor.

## Known simplifications (revisit before the final demo)
- Conversions are always USD -> obligation currency. Surplus GBP/EUR counts toward the floor but is
  never converted back.
- Rates are static all-in client rates from `scenario.json`; the executor must confirm with a real quote.
- The floor is one pooled USD-equivalent number, not per currency.
- Mandate amounts are a policy budget; the sandbox wallets hold far more.
- Brute force is exact for 5 obligations; the simulator with 30+ needs a solver backend (the planner raises
  `NotImplementedError` rather than silently degrading).