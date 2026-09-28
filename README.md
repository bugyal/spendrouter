# spendrouter

Route every model call to the cheapest acceptable spend tier — and refuse the
call outright when it would break a cap you set.

Subscription quota is the cheapest tier. Deferred spend is a scarce budget.
Pay-as-you-go is the last resort. Local inference is free. Most teams juggle
those four tiers in a spreadsheet and find out they overspent afterwards;
spendrouter makes the choice per call, before the money moves.

```
$ spendrouter route --project work --model claude-opus-5 --tokens 200000 --at 2026-09-28T02:00:00+00:00
ALLOW  project=work  model=claude-opus-5  ~140,000 in / 60,000 out tokens
  reason: cheapest tier inside every cap — deferred window open, discount x0.5
  schedule: deferred window open: cheapest slot of the day (2026-09-28 02:00 UTC)
  tier: deferred   effective cost this call: $0.12

Dry run: nothing written. Re-run with --commit once the call actually happens.
```

The same call at 10:00 — a peak hour — lands on a different tier and costs 25x
more, and the router says exactly why:

```
$ spendrouter route --project work --model claude-opus-5 --tokens 200000 \
    --at 2026-09-28T10:00:00+00:00 -v
ALLOW  project=work  model=claude-opus-5  ~140,000 in / 60,000 out tokens
  reason: cheapest tier inside every cap — metered, always available
  schedule: peak hour: subscription quota x3.5 (2026-09-28 10:00 UTC)
  tier: payg   effective cost this call: $3.00
  tier quotes:
    subscription  $ 56.0000 effective  (2800.00 quota, $0.0000 cash)  x3.5
    deferred      unavailable — outside every deferred window
    payg          $  3.0000 effective  (   0.00 quota, $3.0000 cash)
  caps (after this call):
    [ok] hourly spend cap: 0.00/5.00 used (0%), this call +3.00
    [ok] weekly spend cap: 0.00/40.00 used (0%), this call +3.00
```

## Why this exists

Two facts make the naive "just use the best model" approach expensive:

1. **Peak hours burn subscription quota faster.** Community reports put the
   same work at ~3-4x quota during provider peak windows. Quota that looked
   ample at 08:00 is gone by 15:00.
2. **Deferred-spend windows move.** "Off-peak is cheap" is not a policy; the
   windows shift weekly, and a router that hardcodes them is wrong within a
   month.

So the router consults a **schedule table you own** instead of guessing, and it
enforces **hard caps** rather than reporting them.

## Install

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/spendrouter --help
```

Python 3.11+ (uses `tomllib` from the stdlib — no runtime dependencies).

## Configure

Copy `examples/spendrouter.toml` and replace the prices with your own plan's
numbers. Then either put it at `./spendrouter.toml` or set
`SPENDROUTER_CONFIG=/path/to/it`.

```toml
[models.claude-opus-5]
subscription = { quota_per_1k = 4.0 }   # quota units burned per 1k tokens
deferred     = { usd_per_mtok = 1.20 }
payg         = { usd_per_mtok = 15.00 }
local        = true                      # zero marginal cost

[projects.work]
allowed_models = ["claude-opus-5", "glm-5.3-flash"]
tier_order = ["subscription", "deferred", "payg", "local"]
hourly_usd = 5.00
weekly_usd = 40.00
deferred_budget_usd = 25.00
peak_hours = [9, 10, 11, 12, 13, 14, 15, 16, 17]
peak_multiplier = 3.5
deferred_windows = ["00:00-06:00", "22:00-23:59"]
deferred_multiplier = 0.5
```

Every cap is optional. Omit one and that dimension is simply uncapped — the
router will say `(no cap)` rather than inventing a limit.

## Commands

| command | what it answers |
|---|---|
| `route` | which tier serves this call, and what it costs — the go/no-go |
| `plan` | how many calls of this size you can afford before a cap stops you |
| `status` | rolling hourly/weekly quota and spend against your caps, right now |
| `ledger` | per-day rollup and recent events |
| `caps` | the effective limits for a project |

### Pre-flight a long run before it bills

`--dry-run` is the default; nothing is written until the call actually happens.

```
$ spendrouter plan --project work --model claude-opus-5 --tokens 250000 --calls 20 \
    --at 2026-09-28T10:00:00+00:00
plan: project=work model=claude-opus-5 calls=20 (~250,000 tokens each)
  affordable calls: 1
  tiers used: payg x1
  estimated effective cost: $3.75
  estimated quota burn:     0.00 units
  estimated cash spend:     $3.75
  stopped at call 2: refused: every available tier would break a hard cap
  (hourly spend cap: 3.75/5.00 used (75%), this call +3.75)
```

That last line is the point: the job is stopped **before** it bills, and the
message names the exact cap that stopped it. Note the plan is read-only — it
simulates against a throwaway copy of the ledger and never charges your real one.

### Record what actually happened

Estimates drive the go/no-go decision, but the ledger must hold real numbers or
the next decision is made against fiction. `--commit` scales the estimate to the
actual token counts:

```sh
spendrouter route --project work --model claude-opus-5 --tokens 200000 --commit \
  --actual-tokens-in 183400 --actual-tokens-out 41200
```

### Exit codes

`0` allowed · `3` refused (a cap would break) · `4` config error.

A refusal is a normal, expected outcome — wire `3` into your job runner and let
the run stop instead of blowing the budget.

### JSON output

Every subcommand takes `--json` for machine-readable output, so a harness can
read the decision without parsing prose.

## Design notes

- **Tier order is the policy, effective cost is the evidence.** Routing follows
  your declared `tier_order`, not a raw dollar sort — subscription quota is a
  *sunk* cost, and comparing "400 quota units" to "$1.00 of real cash" is not
  like-for-like. Effective cost is computed and shown for every tier so the
  trade is visible, and a project that disagrees just reorders its tiers.
- **Caps only block what they constrain.** A pay-as-you-go call burns no
  subscription quota, so an exhausted quota cap cannot block it — otherwise a
  project with no quota left would be starved of the one tier that cannot make
  the situation worse.
- **Rolling windows, not calendar days.** Caps are checked against the last hour
  and the last 7 days, so a runaway job is caught mid-flight instead of being
  discovered at midnight.
- **Refusal beats a warning.** If every available tier would break a cap, the
  call is refused and nothing is charged. There is no "proceed anyway" flag.
- **Zero telemetry.** The ledger is a local sqlite file. Nothing leaves the box.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The suite covers the parts that matter: tier selection and fail-over, the peak
multiplier pushing a call off the subscription tier, deferred-window
availability, cap enforcement stopping a runaway job at the right call, estimate
reconciliation against real token counts, and config validation errors.

```
56 passed
```

## License

Apache-2.0.
