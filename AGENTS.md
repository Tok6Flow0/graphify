## Mandatory Token Efficiency

- For any request beyond a very small, single-step answer or command, invoke and follow the repo-local `$token-efficient-coordinator` skill before broad exploration or implementation.
- Treat "very small" as a request that can be answered directly without repo exploration, multi-file reads, planning, subagents, or code/docs edits.
- For non-trivial work, read `graphify-out/GRAPH_REPORT.md` first and use Graphify outputs to choose the smallest useful file set.
- Use the highest-cost model only for coordination, ambiguity resolution, risk review, and final synthesis.
- Delegate narrow, independent scans or extraction to lower-cost workers when doing so is cheaper without reducing quality.
- After code, docs, skill, or site edits, run `./scripts/graphify.sh update .` or the repo-root fallback and commit updated `graphify-out/*`.
- For non-trivial work, run `python3 scripts/token_efficiency.py record ...` before final handoff and commit the new `.codex/token-usage/events/*` file with the work. Store concise summaries/tags only, never raw secrets or full transcripts.
- After merging branch work into `main`, run `python3 scripts/token_efficiency.py merge --write-summary` so `.codex/token-usage/ledger.jsonl` and `.codex/token-usage/summary.json` are regenerated from branch-safe event files.

## Mandatory Skill Anatomy

- For any request that creates, edits, reviews, or installs a skill, invoke and follow the repo-local `$skill-anatomy-rulebook` skill before changing skill files.
- Treat `.codex/skills/skill-anatomy-rulebook/references/skill-anatomy.md` as the local source copy of the Skill Anatomy rules.
- Repo-local skills must live under `.codex/skills/<skill-name>/` unless the user explicitly asks for a different skill root.
- Every changed `SKILL.md` must keep valid YAML frontmatter with only `name` and `description`, a description that includes `Use when` triggers, actionable workflow guidance, anti-rationalization checks, red flags, and verifiable exit criteria.
- Validate changed skills before commit with `/Users/aaronsamuel/.codex/skills/.system/skill-creator/scripts/quick_validate.py <path-to-skill-folder>` or an equivalent frontmatter/name check.
## graphify

This project has a knowledge graph at `graphify-out/`.

Before codebase questions, read:
- `graphify-out/GRAPH_REPORT.md` first.
- If `graphify-out/wiki/index.md` exists, use it for broad navigation.

After code/docs/skill/site edits, run:
- `./scripts/graphify.sh update .`

If the local launcher is missing, reroute through the repo root:
- `ROOT_DIR="$(git rev-parse --show-toplevel)"`
- `"${ROOT_DIR}/scripts/graphify.sh" update .`

Commit updated `graphify-out/*` files with related changes.
