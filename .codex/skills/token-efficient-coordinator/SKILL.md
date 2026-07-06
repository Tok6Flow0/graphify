---
name: token-efficient-coordinator
description: Plan and execute Codex work with graph-first repository exploration, model routing, low-cost subagents, prompt budgeting, lightweight token-usage telemetry, branch-safe tracking merges, visual usage reports, and post-change graph refresh. Use for repo exploration, multi-file implementation, debugging, refactoring, code review, architecture analysis, Railway audits, or any task where GPT-5.5 should coordinate cheaper GPT-5.4-mini, GPT-5.4-nano, or Spark workers without sacrificing quality.
---

# Token Efficient Coordinator

## Overview

Coordinate non-trivial Codex work so expensive reasoning is reserved for decisions that need it, while graph-first exploration, low-cost workers, and lightweight telemetry keep repeated repo work efficient.

## When to Use

- Use for repo exploration, multi-file implementation, debugging, refactoring, review, architecture analysis, Railway audits, and any request that benefits from graph-guided file selection or worker delegation.
- Use when tracking or visualizing token usage for repo work.
- Do not use for a very small, single-step answer or command that needs no repo exploration, planning, edits, or subagents.

## Objective

Use the most expensive reasoning only where it pays for itself. Keep `gpt-5.5` focused on coordination, ambiguity resolution, risk review, and final synthesis. Push narrow, graph-scoped discovery and extraction work to cheaper workers.

Use bundled references when needed:
- `references/token-efficiency-research.pdf`
- `references/model-routing-rate-card.md`
- `references/token-usage-tracking.md`

## Non-Negotiable Rules

1. Graph before grep.
2. Graph before broad file reads.
3. Graph before spawning workers.
4. After code, docs, skill, or site edits, update graphify output or confirm the hook did it.
5. Give smaller models narrower scopes, explicit file lists, and strict output contracts.
6. Keep stable instruction prefixes stable so prompt caching can work.
7. Do not let multiple workers read the same large file set unless independent review is intentional.
8. For every non-trivial run, record one lightweight token-efficiency event before the final handoff.
9. After branch work is merged to `main`, regenerate token summaries from event files instead of manually merging summaries.

## Graphify Workflow

This repo family uses Tok6Flow0/graphify to map code, docs, schemas, scripts, PDFs, and infrastructure into `graphify-out/GRAPH_REPORT.md`, `graphify-out/graph.json`, and related graph artifacts.

At the start of repo-facing work:

1. Read `graphify-out/GRAPH_REPORT.md`.
2. If available, use:
   - `graphify query "<question>"`
   - `graphify path "<symbol_a>" "<symbol_b>"`
   - `graphify explain "<symbol_or_concept>"`
3. Extract relevant subsystems, connected symbols, likely blast radius, and the smallest file set.
4. Spawn workers with the graph slice and exact file list only.

After edits:

```bash
./scripts/graphify.sh update .
```

Fallback:

```bash
ROOT_DIR="$(git rev-parse --show-toplevel)"
"${ROOT_DIR}/scripts/graphify.sh" update .
```

Commit updated `graphify-out/*` with related changes.

## Model Alias Map

Use these aliases in planning:

| Alias | Model setting | Best use |
|---|---|---|
| `frontier_deep` | `gpt-5.5`, `reasoning.effort: xhigh` | Hard architecture, subtle bugs, security, last-resort coordinator |
| `frontier_balanced` | `gpt-5.5`, `reasoning.effort: medium` | Coordinator, synthesis, final plan, merge worker results |
| `frontier_reviewer` | `gpt-5.5`, `reasoning.effort: high` | Risk review, cross-file correctness, final validation |
| `standard_balanced` | `gpt-5.4`, `reasoning.effort: medium` | Non-trivial coding when 5.5 is unnecessary |
| `light_worker` | `gpt-5.4-mini`, `reasoning.effort: low` | Repo scans, file triage, local audits, document review |
| `atomic_worker` | `gpt-5.4-nano`, `reasoning.effort: none or low` | Extraction, classification, ranking, dedupe, strict JSON transforms |
| `realtime_editor` | `gpt-5.3-codex-spark` | Fast text-only iteration and focused edits |

Normalize user shorthand:

- "5.5 extra high" means `frontier_deep`.
- "5.4 light" means `light_worker` by default.
- For purely mechanical extraction or ranking, downgrade "5.4 light" to `atomic_worker`.
- Do not plan new ChatGPT-sign-in workflows around deprecated Codex models such as `gpt-5.3-codex`.

## Rate Card

Use the rate card from the research reference and verify current pricing when accuracy matters.

API/batch-style price hints per 1M tokens:

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-5.5` | 5.00 | 0.50 | 30.00 |
| `gpt-5.4` | 2.50 | 0.25 | 15.00 |
| `gpt-5.4-mini` | 0.75 | 0.075 | 4.50 |
| `gpt-5.4-nano` | 0.20 | 0.02 | 1.25 |

Codex credit hints per 1M tokens:

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-5.5` | 125 | 12.5 | 750 |
| `gpt-5.4` | 62.5 | 6.25 | 375 |
| `gpt-5.4-mini` | 18.75 | 1.875 | 113 |

Spark has preview limits; do not assume normal API economics for it.

Formula:

```text
estimated_cost = (input_mtok - cached_input_mtok) * input_rate
  + cached_input_mtok * cached_input_rate
  + output_mtok * output_rate
```

Prefer a worker swarm whenever total estimated worker cost plus coordinator cost is below a monolithic `gpt-5.5` pass, even by a small margin. Default threshold: choose the swarm if estimated total is at most 0.99 times the monolithic plan; a 1% estimated savings is worth taking because repeated small savings compound. Quality still overrides cost for security, data-loss, money movement, auth, migrations, and subtle correctness, but do not require a large savings margin for routine exploration, extraction, audits, or bounded implementation.

## Lightweight Usage Tracking

Use `scripts/token_efficiency.py` for local, low-overhead telemetry. Prefer provider-reported usage numbers when they are already available; otherwise use the script's local estimator. Do not spend extra model calls only to track tokens.

Record one event for every non-trivial run:

```bash
python3 scripts/token_efficiency.py record \
  --task "<short task summary>" \
  --kind "<bugfix|feature|audit|skill-update|review|ops>" \
  --tag "<topic-or-system>" \
  --change "<short change label>" \
  --alias frontier_balanced \
  --input-text "<short task/request summary>" \
  --visible-output-tokens <rough_final_output_tokens> \
  --graph-consulted yes \
  --graph-refreshed yes \
  --validation "<command or check>"
```

Rules:

- Store short task summaries, tags, change labels, model, effort, graph status, validation, and token estimates.
- Do not store full prompts, transcripts, secrets, credentials, bearer tokens, private keys, customer data, or raw production payloads.
- Use `--input-tokens`, `--cached-input-tokens`, and `--output-tokens` when exact provider usage is known.
- Use `--worker light_worker:2` or similar when subagents were spawned.
- Include `python3 scripts/token_efficiency.py report --compact` output in final run reports when useful.

Branch-safe merge routine:

```bash
python3 scripts/token_efficiency.py merge --write-summary
```

The source of truth is immutable per-run JSON under `.codex/token-usage/events/`. `ledger.jsonl` and `summary.json` are regenerated artifacts. If a branch merge conflicts in `ledger.jsonl` or `summary.json`, keep either side, run the merge command above on `main`, and commit the regenerated files.

Optional visuals:

```bash
python3 scripts/token_efficiency.py visualize
```

The visualizer must produce a reader-useful dashboard, not isolated sparse charts. It writes:

- `index.html`: a compact report-style dashboard with executive summary, KPI cards, prompt-level timeline, last-7-days rollup, average tokens per prompt, branch/model/kind/tag concentration, recent prompt ledger, and caveats.
- `dashboard-data.json`: bounded dashboard data for audit or downstream report widgets.
- Static SVG chart assets for prompt sequence, 7-day daily rollup, branch, model, task kind, and tag usage.
- `remotion-token-efficiency-data.json` and a callable Remotion package at `.codex/token-usage/visuals/remotion/`.

Do not collapse multiple same-day prompts into one unexplained point. Daily charts are rollups only; always preserve prompt-level rows and a prompt sequence chart. The 7-day view should include empty days and show total tokens, prompt count, and average tokens per prompt. Use `--pdf` or `--pdf-path` when the user wants a static PDF export:

```bash
python3 scripts/token_efficiency.py visualize --pdf
```

PDF export uses Chrome/Chromium when available and keeps the HTML dashboard as the source of truth. Preview Remotion with `npm install && npm run studio`, render one frame with `npm run still`, or export video with `npm run render`.

## Routing Policy

Use `frontier_balanced` or `frontier_deep` for:

- merging worker results
- resolving conflicting evidence
- architecture decisions across modules
- security, credentials, payments, trading, or data-loss risk
- final user-facing synthesis

Use `light_worker` for:

- graph-scoped repo exploration
- read-only Railway/config scans
- identifying candidate files
- summarizing a bounded subsystem
- checking docs, runbooks, and generated reports

Use `atomic_worker` for:

- extracting keys from logs/JSON
- classifying files by owner or risk
- building rename maps
- deduplicating findings
- producing strict small JSON outputs

## Worker Task Cards

Give smaller models short, explicit task cards.

Scan worker:

```text
Goal:
Scope:
Relevant graph findings:
Files allowed to read:
Questions to answer:
Forbidden actions:
Return format:
- relevant files
- findings
- uncertainties
- recommended escalation yes/no
```

Atomic worker:

```text
Goal:
Input set:
Transformation required:
Rules:
Return JSON schema:
Stopping condition:
```

Reviewer worker:

```text
Goal:
Change set:
Risks to inspect:
Validation required:
Return format:
- findings
- severity
- confidence
- evidence
```

## Escalation

Escalate upward when:

- the worker sees ambiguity outside its provided graph slice
- the blast radius grows beyond the assigned files
- security, credentials, payments, trading, or data-loss logic appears
- workers disagree
- migrations, concurrency, or subtle correctness are involved

Ladder: `atomic_worker` -> `light_worker` -> `standard_balanced` -> `frontier_balanced` -> `frontier_deep`.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I already know the repo, so I can skip Graphify." | Graphify is the cheapest current map and prevents broad repeated reads. |
| "Subagents are overhead unless savings are large." | Small savings compound; use worker swarms when the estimate is at most 0.99x the monolithic plan and risk is bounded. |
| "Token logging costs more than it saves." | The local script records concise metadata and avoids extra model calls. |
| "Daily token charts are enough." | Daily charts hide prompt-level behavior; preserve prompt sequence and daily rollups. |
| "Summaries can be merged manually." | Branch-safe event files are the source of truth; regenerate summaries from events. |

## Red Flags

- Broad `rg`, `find`, or file reads before checking `graphify-out/GRAPH_REPORT.md`.
- Multiple workers reading the same large file set without an intentional review reason.
- A smaller model receives a vague, long, or cross-cutting task card.
- Edits are committed without refreshing graph outputs when the tree is otherwise safe to refresh.
- `.codex/token-usage/ledger.jsonl` or `summary.json` is hand-merged instead of regenerated.
- A dashboard groups all same-day prompts into one point without prompt-level rows.

## Verification

Before finishing non-trivial work, confirm:

- [ ] Graph consulted first or the exception is documented.
- [ ] Worker routing decision was made with scope, cost, and risk in mind.
- [ ] Changed files were validated with relevant tests/checks.
- [ ] Graphify output was refreshed after edits, or skipped to avoid touching unrelated dirty work.
- [ ] A lightweight token event was recorded when the repo state allowed it.
- [ ] Branch-safe token summaries were regenerated after merges to `main` when applicable.

## Run Report

Before finishing, report:

- graph consulted first: yes/no
- graph refreshed after edits: yes/no
- token usage event recorded: yes/no
- token report command/output if relevant
- models/workers used and why
- estimated cost reasoning
- duplicated reads avoided
- escalations
- validation performed
