---
name: skill-anatomy-rulebook
description: Guides agents through creating, updating, and reviewing repo-local skills with required SKILL.md anatomy, trigger metadata, progressive disclosure, validation, and anti-rationalization checks. Use when adding a new skill, editing an existing skill, reviewing skill quality, or changing bundled skill references/scripts/assets.
---

# Skill Anatomy Rulebook

## Overview

Create skills as compact executable workflows, not loose documentation. A good skill gives a future agent enough process, evidence requirements, and local resources to perform the task without re-discovering the same rules.

The source rulebook is bundled at `references/skill-anatomy.md`. Use it for exact formatting details when creating a new skill or making structural changes.

## When to Use

- Use when creating a new repo-local skill under `.codex/skills/<skill-name>/`.
- Use when updating an existing `SKILL.md`, skill reference, script, asset, or `agents/openai.yaml`.
- Use when reviewing whether a skill has clear triggers, workflow steps, anti-rationalization guidance, and verification.
- Do not use for ordinary app code, docs, or config changes unless they affect skill behavior.

## Core Process

1. Locate the repo-local skill root. In this repo family, use `.codex/skills/<skill-name>/`; if external docs say `skills/<skill-name>/`, adapt that to `.codex/skills/<skill-name>/`.
2. Keep the directory name lowercase and hyphen-separated. The frontmatter `name` must match the directory exactly.
3. Write only `name` and `description` in YAML frontmatter. The description must state what the skill does and include specific `Use when` triggers.
4. Structure `SKILL.md` around the standard pattern unless an equivalent heading is clearer:
   - `Overview`
   - `When to Use`
   - `Core Process`, `Workflow`, or `Steps`
   - `Specific Techniques` or equivalent scenario guidance
   - `Common Rationalizations`
   - `Red Flags`
   - `Verification`
5. Move long material into `references/`, runnable helpers into `scripts/`, and reusable source material into `assets/` only when those resources are actually needed.
6. Create or update `agents/openai.yaml` with concise display metadata after reading the finished skill.
7. Validate every changed skill before committing.

## Specific Techniques

- Keep `SKILL.md` under 500 lines. If reference material exceeds about 100 lines, put it in `references/` and link it from the workflow.
- Put trigger conditions in the frontmatter description, because that is what agents see before the skill body loads.
- Prefer imperative, testable instructions: `Run <command> and verify <result>` beats vague guidance like `make sure it works`.
- When adding scripts, use a bash shebang, `set -e`, status output on stderr, machine-readable output on stdout, and cleanup traps for temp files.
- Do not create empty `scripts/`, `references/`, or `assets/` directories just to mirror another skill.
- Cross-reference other skills by name instead of duplicating their content.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The skill is mostly documentation, so frontmatter does not matter." | Frontmatter is the activation contract. Weak triggers cause the skill to be missed or used incorrectly. |
| "This task is simple enough to skip Verification." | Verification is the exit contract; without it, future agents cannot tell whether the workflow was completed. |
| "A long SKILL.md is more complete." | Long always-loaded instructions waste context. Move deep reference material into linked files. |
| "Empty resource folders make the skill look standardized." | Empty folders add noise and do not improve agent behavior. |
| "The attached source doc says `skills/`, so repo-local skills must live there." | This repo family uses `.codex/skills/`; preserve the local discovery convention. |

## Red Flags

- Frontmatter has fields beyond `name` and `description`.
- `description` lacks a clear `Use when` trigger.
- Directory name and frontmatter `name` differ.
- The workflow reads like background knowledge instead of steps.
- No `Common Rationalizations`, `Red Flags`, or `Verification` section exists.
- Supporting files are chained through multiple references instead of linked directly from `SKILL.md`.
- Scripts are untested or emit human prose where downstream automation expects JSON.

## Verification

After skill changes, confirm:

- [ ] The changed skill directory name matches frontmatter `name`.
- [ ] Frontmatter includes only `name` and `description`.
- [ ] `description` includes both what the skill does and when to use it.
- [ ] `SKILL.md` has clear workflow, rationalization, red flag, and verification guidance.
- [ ] Supporting files are direct, necessary, and not empty placeholders.
- [ ] Any changed scripts were run or a reason is documented.
- [ ] `quick_validate.py` or an equivalent frontmatter/name validator passes for each changed skill.
