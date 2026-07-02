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
