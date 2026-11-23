# Using Agent Audit Kit

> *Synthetic test harness. The reference agent is a test target, not a screening tool; the PHQ-9 is
> a depression measure, not a suicide-risk assessment; nothing here is clinical advice or a medical
> device.*

The reference for running the kit. The [README](../README.md) gives the overview and the published
run's results. The core is the same for every domain; each domain is a pack, a folder with the
agent's prompt and tools, its risks, its cases and its rules. `--domain phq9` picks the first one,
a PHQ-9 intake agent. Writing a pack for your own domain: [`DOMAIN_PACKS.md`](DOMAIN_PACKS.md).

## How a verdict is reached

The kit applies methods from regulated QA (ISO 14971 style): hazard → risk control → check → result.

1. **Hazards first.** A hazard registry lists each way the agent can cause harm: its failure mode,
   the hazardous situation, the harm, a severity, the risk controls meant to prevent it, and
   acceptance criteria. The criteria are declared before the run, and their hash goes into the
   run's manifest.
2. **Synthetic users, exact inputs.** Each case is an invented user, tagged with the hazards it
   probes. A simulated user plays it, but a disclosure or an injection is sent word for word, so the
   simulator cannot soften it. A test-data lint checks every case and every link before any API
   call.
3. **Repeated trials.** Each case runs several times against the agent, under a hard budget cap.
   Every turn and every tool call is recorded.
4. **Deterministic grading.** Checks linked to hazards and controls grade each conversation and
   point at the turns that show a failure. Validity checks decide whether a trial counts: a
   simulated user that drifted from its case, or a test input never delivered, says nothing about
   the agent.
5. **Verdicts with their evidence.** Per hazard, over distinct cases (never pooled repeats): the
   observed failure rate, an upper bound on it, and a verdict against the declared criterion. The
   rule is in the pack guide, under [Hazards and cases](DOMAIN_PACKS.md#4-hazards-and-cases).
6. **Replayable evidence.** Every run keeps its transcripts and a byte copy of the pack. A replay
   re-grades them with no API call, so anyone can check a report against its run folder.

## Setup

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 and the dependencies.

```bash
uv sync
```

Live runs call the Anthropic API. Put your key in a `.env` file at the project root. This file is
git-ignored, so it is never committed:

```bash
ANTHROPIC_API_KEY=sk-ant-...
# optional, shown with their defaults:
# the agent under test
AGENT_MODEL=claude-haiku-5-5
# the simulated user
SIMULATOR_MODEL=claude-haiku-5-5
# hard cap per run, in USD
EVAL_BUDGET_USD=3.00
```

Put each comment on its own line: a `# comment` after a value becomes part of the value.

> **`eval` is a shell builtin.** Typing `eval --domain phq9` on its own runs your shell's `eval`,
> not this tool. Always go through uv: `uv run eval …`. The same command is also installed as
> `auditkit` (`uv run auditkit …`).

## Live runs

```bash
uv run eval --lint phq9                   # check the pack's test data, print the cost estimate; no API call
uv run eval --domain phq9 --repeats 1     # a live smoke run: one trial per case (costs money)
uv run eval --domain phq9 --budget-usd 10 # the full run, 3 trials per case: above the $3 default cap (costs money)
```

A live run (`--domain`) lints the pack and prints its cost estimate before anything is sent. It is
refused if the lint finds an error, or if the estimate is above the cap.

Useful options:
- `--cases a,b`: run only these cases;
- `--repeats N`: trials per case;
- `--target NAME`: the agent under test, from the pack's `targets/`;
- `--variant NAME`: another set of cases and data;
- `--budget-usd USD`: the cap for this run;
- `--packs-dir DIR`: packs kept outside this repository.

`uv run eval --help` lists every option.

An interrupted run, or one stopped by its cap, can be finished with
`uv run eval --resume runs/<timestamp>`. Completed trials are kept, and the remaining ones cost
money.

## The run folder

A live run writes a folder like this one:

```
runs/<timestamp>/
├── manifest.json        # what ran: the models, and fingerprints of the prompt, suite and criteria
├── execution.json       # its status, the money spent, and a hash of each transcript
├── snapshot/phq9/       # a byte copy of the pack as it was run
├── lint.json            # the lint's warnings at the start of the run
├── expectations.json    # the right answers derived for each case, such as its total and band
├── <case>/run_<n>.json  # one transcript per trial: raw evidence, written once
├── results.json         # every check result, per-case statistics, per-hazard verdicts
├── report.md            # the risk-based evaluation report
├── history/<time>/      # the earlier results.json and report.md, after a replay
└── regrade-<time>/      # results.json and report.md from a regrade with today's pack
```

A transcript holds every turn and tool call, what the simulated user meant to answer, and each
model call with its cost.

## Replay and regrade

```bash
uv run eval --replay runs/<timestamp>
```

A replay re-grades the saved transcripts with the run's own snapshot of the pack: the same checks,
data and criteria. It makes **no API call**, and it never runs a tool again. First, it checks every
transcript and the snapshot against the hashes recorded at run time, and stops with exit code 5 if
anything changed. It then writes a new `results.json` and `report.md`, and moves the previous pair
to `runs/<timestamp>/history/`.

To grade old evidence with the pack as it is now (after fixing a check, for example), add
`--regrade-current`. That writes `runs/<timestamp>/regrade-<time>/` and leaves the run's own report
untouched.

## The local page

```bash
uv run eval --ui                         # serves http://127.0.0.1:8765/ and opens it; Ctrl+C stops it
uv run eval --ui --port 0 --no-browser   # any free port; print the address only
```

A local page for the same work, with nothing to install:
- **Results**: the runs. For each run, a matrix of hazards × cases: passed ✓, failed ✕, excluded
  (too few valid trials), or not tagged. It also shows each hazard's verdict, controls, checks and
  criterion, and the report's top findings and limits. A case opens down to each check's result
  and the transcript. A replay is free.
- **Run**: pick a pack, cases, repeats and a cap, then get the lint and cost estimate (free). A
  live run starts only after a confirmation that names its trials, models, typical and worst cost,
  and cap. The page then shows the run's log as it goes.
- **Parameters**: the pack's settings, the models and the budget a run would use (and where each
  comes from), targets, hazards and cases.

The page runs `eval` itself for live runs and replays, so the same lint, estimate, cap and exit
codes apply. It runs one at a time. To stop a running run, press Ctrl+C in the terminal, then
finish it with `--resume`. The server reads `.env` at start: restart it after a change.

The server listens on 127.0.0.1 and answers only its own address (`127.0.0.1` or `localhost`, on its
port). Every request for its data or to start a run needs a token that the page receives when it
loads and that changes with every start. So another web page open in the same browser cannot read
results or start a paid run. The page loads nothing from the internet, and shows transcripts as
text, never as HTML.

## Cost

Defaults:
- both the agent and the simulated user run on `claude-haiku-5-5`;
- prices are dated in `src/auditkit/cost.py`: $0.10 per million input tokens and $0.50 per million
  output tokens as of 2026-10-06;
- each run has a hard cap of $3.00 (`EVAL_BUDGET_USD` or `--budget-usd`).

Before any call, the estimate is compared with the cap. During the run, a call that could cross the
cap is not made, and the trials already completed are kept.

`uv run eval --lint <pack>` prints the estimate for free, per role, typical and worst case. For the
current PHQ-9 pack (177 cases × 3 repeats), it says about $4.68 typical and $9.56 worst case. That
is above the $3.00 default cap, so a full run needs `--budget-usd`. It does not count thinking
tokens. The published run, 531 conversations, cost $3.36.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | report written, whatever the verdicts |
| 2 | usage, configuration or test-data error: nothing sent |
| 3 | budget: estimate above the cap, or cap reached (partial report written) |
| 4 | harness or provider error (partial report written) |
| 5 | replay or resume integrity error: a snapshot or transcript does not match its hash |
| 130 | interrupted (Ctrl-C): completed trials are kept; finish with `--resume` |

## Development

```bash
uv run pytest                                      # the harness's self-tests and the packs' own tests; offline
uv run ruff check . && uv run ruff format --check .
```

The tests never touch the network: a scripted model stands in for the API.

## Repository layout

```
src/auditkit/   the domain-agnostic core: it never imports a pack
domains/        domain packs: hazards, cases, targets, mocked tools, checks (domains/phq9/)
docs/           USAGE.md (this file), DOMAIN_PACKS.md (how to write a pack), index.html (the run's page)
tests/          the harness's self-tests; tests/fixtures/packs/toypack is the minimal pack
tools/          gen_confusables.py (the look-alike table), gen_page.py (the page's grid and curve)
examples/       published runs, with their transcripts, to replay (examples/README.md)
runs/           run outputs (git-ignored)
```
