# spendrouter

One local tool for agent spend, in two layers:

- **ROUTE**: *before* a call, send it to the cheapest acceptable spend tier
  (subscription quota → deferred → pay-as-you-go → local) and refuse it
  outright when it would break a cap you set.
- **CONTAIN**: *while* agents run, `spendrouter serve` sits between your
  agents and the provider APIs. It ledgers every real call, failed and
  retried ones included. It pauses an agent caught in a loop, refuses
  calls over a hard cap before they reach the provider, and hands agents
  scoped, expiring `sr_` credentials instead of your provider keys.

Both layers read one config file (`spendrouter.yml`) and write one sqlite
ledger. The runtime is stdlib-only Python with zero telemetry: nothing
leaves the box.

## Install

The code needs **Python 3.11+** and has no runtime dependencies. On macOS
`python3` is often the 3.9 system build, whose pip is also too old for
editable installs, so pick a versioned interpreter:

```sh
python3.13 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/spendrouter --help
```

Check first if you are unsure:

```sh
python3 -c 'import sys; print(sys.version_info >= (3, 11))'   # want: True
```

Run the CLI as `.venv/bin/spendrouter` (or activate the venv), or as
`python -m spendrouter.cli`. Running `./src/spendrouter/cli.py` directly will
not work: it is a package, and it resolves its own imports relatively.

## ROUTE: pick the tier before the call

Subscription quota is the cheapest tier. Deferred spend is a scarce budget.
Pay-as-you-go is the last resort. Local inference is free. Most teams juggle
those four tiers in a spreadsheet and find out they overspent afterwards;
spendrouter makes the choice per call, before the money moves.

### Quick start

```sh
spendrouter init     # writes a commented spendrouter.yml — put your plan's prices in it
spendrouter route --project work --model claude-opus-5 --tokens 200000
```

```
$ spendrouter route --project work --model claude-opus-5 --tokens 200000 --at 2026-09-28T02:00:00+00:00
ALLOW  project=work  model=claude-opus-5  ~140,000 in / 60,000 out tokens
  reason: cheapest tier inside every cap — deferred window open, discount x0.5
  schedule: deferred window open: cheapest slot of the day (2026-09-28 02:00 UTC)
  tier: deferred   effective cost this call: $0.12

Dry run: nothing written. Re-run with --commit once the call actually happens.
```

The same call at 10:00, a peak hour, lands on a different tier and costs 25x
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

### Why route

Two facts make the naive "just use the best model" approach expensive:

1. **Peak hours burn subscription quota faster.** Community reports put the
   same work at ~3-4x quota during provider peak windows. Quota that looked
   ample at 08:00 is gone by 15:00.
2. **Deferred-spend windows move.** "Off-peak is cheap" is not a policy; the
   windows shift weekly, and a router that hardcodes them is wrong within a
   month.

So the router consults a **schedule table you own** instead of guessing, and it
enforces **hard caps** rather than reporting them.

### Configure the route layer

```yaml
models:
  claude-opus-5:
    subscription: {quota_per_1k: 4.0}   # quota units burned per 1k tokens
    deferred: {usd_per_mtok: 1.20}
    payg: {usd_per_mtok: 15.00}
  qwen3-coder-30b-local:
    local: true                         # zero marginal cost

projects:
  work:
    allowed_models: [claude-opus-5, qwen3-coder-30b-local]
    tier_order: [subscription, deferred, payg, local]
    hourly_usd: 5.00
    weekly_usd: 40.00
    deferred_budget_usd: 25.00
    peak_hours: [9, 10, 11, 12, 13, 14, 15, 16, 17]
    peak_multiplier: 3.5
    deferred_windows: ["00:00-06:00", "22:00-23:59"]
    deferred_multiplier: 0.5
```

Every cap is optional. Omit one and that dimension is simply uncapped; the
router says `(no cap)` rather than inventing a limit.

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
message names the exact cap that stopped it. The plan is read-only. It
simulates against a throwaway copy of the ledger and never charges your real
one.

### Record what actually happened

Estimates drive the go/no-go decision, but the ledger must hold real numbers or
the next decision is made against fiction. `--commit` scales the estimate to the
actual token counts:

```sh
spendrouter route --project work --model claude-opus-5 --tokens 200000 --commit \
  --actual-tokens-in 183400 --actual-tokens-out 41200
```

Every route verb takes `--json`, so a harness can read the decision without
parsing prose.

## CONTAIN: hold the line while agents run

A pre-call policy cannot stop an agent that is already running. One team's
customer agent retried a failing tool 31,000 times overnight and burned
$4,700 against a $400/month plan. Global rate limits don't contain a single
agent. Per-agent, per-customer attribution and enforcement on the wire do.

### Quick start

```sh
export OPENAI_API_KEY=sk-...  ANTHROPIC_API_KEY=sk-ant-...
spendrouter serve        # proxy on 127.0.0.1:8787; reads ./spendrouter.yml if there is one

# in another shell: run an agent with a credential minted for it and revoked when it exits
spendrouter run --agent support-bot --customer acme -- python my_agent.py
spendrouter report       # today's spend by customer and agent
```

`run` points the child's SDKs at the proxy (`OPENAI_BASE_URL`,
`ANTHROPIC_BASE_URL`) and puts an `sr_` credential where each provider key
was. The proxy swaps the real key in upstream, so the agent never holds it.

To wire an agent yourself, set its SDK base URL to
`http://127.0.0.1:8787/openai/v1` (OpenAI SDKs), `http://127.0.0.1:8787/anthropic`
(Anthropic SDKs), or `/<name>` for any upstream you configure. Attribute its
calls in one of two ways:

- **a credential**: `spendrouter creds mint --agent support-bot --customer acme --ttl 2h`
  prints an `sr_...` token; use it as the API key. Attribution is pinned to
  it and cannot be spoofed. A credential can carry a USD cap
  (`--max-usd`) and an upstream allow-list (`--upstream`).
- **tag headers**: the agent keeps its own provider key and sends
  `X-Spendrouter-Agent` / `-Customer` / `-Task` / `-Run`. Set
  `proxy.require_credential: true` to refuse raw provider keys outright.

### What it enforces

1. **Attribution ledger**: every call that reached the proxy is recorded
   per agent, customer, task, run and credential, with tokens, cost,
   latency and outcome. Failed, retried and refused calls are included,
   because failures and retries cost real money. Streams (SSE) are relayed
   chunk by chunk and metered as they pass.
2. **Loop circuit breaker**: the same tool failing with the same error
   class more than `max_repeats` times in a row, the same endpoint failing
   with the same upstream error more than `api_error_repeats` times, or a
   byte-identical request resent too often trips the breaker. The agent is
   paused (423), a `breaker_tripped` hook fires, and every later call from
   it is refused without reaching the provider until
   `spendrouter resume --agent ...` (or an optional cooldown).
3. **Budget caps**: per agent, customer, task or globally, per hour, day,
   week or month. A soft cap alerts once per period. A hard cap refuses with
   402 **before** the upstream is called, or pauses the scope
   (`action: pause`) so a human decides.
4. **Scoped, expiring credentials**: minted per agent, customer or task.
   Only their SHA-256 is stored, and a finished `run` revokes its own.
5. **Honest metering**: calls that run no model are ledgered at zero tokens
   and $0 and never estimated. These are GET and DELETE, `/v1/models`,
   `/v1/files`, `/v1/fine_tuning/jobs`, `/v1/batches`, and Anthropic's
   `count_tokens` and message batches. An inference call whose response has
   no usage block is estimated at ~4 chars/token and flagged in reports.
   Unknown models are charged a deliberately high fallback price, so spend
   is over-counted rather than under-counted.

Every pause, cap crossing and breaker trip is an event. Events are written
to the ledger and to `events.jsonl` next to it, and passed to any hooks you
configure (a command, or an HTTP POST to a URL you choose).

### Refusal contract

Refusals use the provider's own error shape and non-retryable statuses, so
SDKs raise instead of retrying into the wall:

| where | field | value |
|---|---|---|
| HTTP header | `X-Spendrouter-Refused` | bare reason: `budget`, `credential`, `breaker`, `paused`, ... |
| OpenAI-shaped body | `error.code` | bare reason |
| Anthropic-shaped body | `error.type` | `spendrouter_<reason>` |

Statuses: 401 bad or expired credential · 402 hard cap or credential cap ·
403 credential not valid for this upstream · 423 paused or breaker tripped.

### Without the proxy

The same engine guards calls your own code makes:

```python
from spendrouter.contain import Containment, SpendBlocked

engine = Containment.load()   # the contain sections of ./spendrouter.yml
with engine.call(agent="support-bot", customer="acme", request=payload) as call:
    response = client.messages.create(**payload)   # SpendBlocked is raised before this if refused
    call.record_response(response)
```

## One config, one ledger

`spendrouter init` writes a commented `spendrouter.yml` with both layers:
`models` / `projects` for ROUTE, and `proxy`, `upstreams`, `breaker`,
`budgets`, `pricing` and `hooks` for CONTAIN. Unknown keys are rejected, so a
misspelt `budgets:` cannot leave an agent uncapped. The file is found via
`--config`, then `$SPENDROUTER_CONFIG`, then `./spendrouter.yml`. A 0.1
`spendrouter.toml` (see `examples/`) still loads unchanged. `db_path` (or
`--db`) moves the single sqlite ledger both layers write to.

The contain verbs also run with no file at all. The proxy then fronts the
built-in OpenAI and Anthropic upstreams with no budgets, and still ledgers
and breaker-checks every call.

```yaml
proxy:
  listen: 127.0.0.1:8787
budgets:
  - {scope: customer, match: "*", period: day, soft_usd: 5, hard_usd: 15}
  - {scope: customer, match: acme, period: month, hard_usd: 400, action: pause}
breaker:
  max_repeats: 10          # same tool, same error class: the 11th in a row pauses the agent
hooks:
  - events: [breaker_tripped, hard_cap_exceeded, paused]
    command: ["/usr/local/bin/page-oncall"]
```

## Commands

| layer | command | what it answers |
|---|---|---|
| route | `route` | which tier serves this call, and what it costs: the go/no-go |
| route | `plan` | how many calls of this size you can afford before a cap stops you |
| route | `status` | rolling hourly/weekly quota and spend against your caps, right now |
| route | `ledger` | per-day rollup and recent routed calls |
| route | `caps` | the effective limits for a project |
| contain | `serve` | run the proxy (`--listen host:port`) |
| contain | `run` | run a command with a credential minted for it, revoked when it exits |
| contain | `creds mint/list/revoke/gc` | scoped, expiring `sr_` credentials |
| contain | `report` | proxied spend by customer, agent, task, model, ...; text, `md` or `--json` |
| contain | `check` | gate a job on the containment state of an agent, customer or task |
| contain | `pause` / `resume` | stop or restart an agent, customer, task or everything |
| both | `init` | write a commented `spendrouter.yml` |

### Exit codes

Shared by both layers, so one harness can gate on either:

`0` ok · `1` error · `2` usage · `3` refused (route: a cap would break;
contain: a hard cap is reached) · `4` config error · `5` paused · `6` soft cap
exceeded.

A refusal is a normal, expected outcome: wire `3` into your job runner and let
the run stop instead of blowing the budget.

## Design notes

- **Tier order is the policy, effective cost is the evidence.** Routing follows
  your declared `tier_order`, not a raw dollar sort. Subscription quota is a
  *sunk* cost, and comparing "400 quota units" to "$1.00 of real cash" is not
  like-for-like. Effective cost is computed and shown for every tier so the
  trade is visible, and a project that disagrees just reorders its tiers.
- **Caps only block what they constrain.** A pay-as-you-go call burns no
  subscription quota, so an exhausted quota cap cannot block it. Otherwise a
  project with no quota left would be starved of the one tier that cannot make
  the situation worse.
- **Rolling windows for routing, calendar periods for containment.** Route caps
  look at the last hour and the last 7 days, so a runaway job is caught
  mid-flight. Contain budgets follow the calendar periods a provider bills
  in.
- **Refuse before the money moves.** If every available tier would break a
  cap, the route call is refused and nothing is charged. A proxied call over
  a hard cap never reaches the provider. There is no "proceed anyway" flag.
- **Every real call counts.** Failed, retried and refused calls are ledgered,
  because a loop's cost lives in exactly those calls.
- **Zero telemetry.** The ledger is a local sqlite file, and hooks go only
  where you point them. Nothing else leaves the box.

## Tests

```sh
.venv/bin/python -m pytest -q
```

The route suite covers tier selection and fail-over, the peak multiplier
pushing a call off the subscription tier, deferred-window availability, cap
enforcement stopping a runaway job at the right call, and estimate
reconciliation. The contain suite drives real HTTP through the proxy to a fake
provider. It covers credential swapping, SSE metering, the 31,000-retry loop
contained at the 11th failure, hard caps refusing before the upstream,
zero-metered non-inference endpoints and the CLI verbs. Both suites check
config validation.

```
223 passed
```

## License

Apache-2.0.
