#!/usr/bin/env python3
"""Fast local graphify updater fallback for environments without graphify dependencies.

The script prefers the installed ``graphify`` Python package when all required
runtime dependencies are available. When unavailable, it emits a minimal but
valid graph/metadata report using only the standard library.
"""
from __future__ import annotations

import json
import re
import hashlib
from collections import defaultdict, Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys


try:
    from graphify.detect import detect as _graphify_detect, save_manifest as _graphify_save_manifest
    from graphify.extract import extract as _graphify_extract
    from graphify.build import build_from_json as _graphify_build_from_json
    from graphify.analyze import god_nodes as _graphify_god_nodes
    from graphify.analyze import surprising_connections as _graphify_surprises
    from graphify.analyze import suggest_questions as _graphify_suggest_questions
    from graphify.report import generate as _graphify_generate
    from graphify.export import to_json as _graphify_to_json

    _FULL_SUPPORT = True
except Exception as exc:
    _FULL_SUPPORT = False
    _FULL_SUPPORT_ERROR = exc


CODE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".cs",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".swift",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".m",
    ".mm",
    ".sh",
    ".bash",
    ".sql",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".md",
}

_EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "bin",
    "obj",
    ".gradle",
    ".next",
    "out",
    "coverage",
    "graphify-out",
    ".idea",
    ".vscode",
    ".cache",
    "tmp",
    "temp",
}


@dataclass
class SimpleGraph:
    nodes: list[dict]
    links: list[dict]

    def number_of_nodes(self) -> int:
        return len(self.nodes)

    def number_of_edges(self) -> int:
        return len(self.links)

    def node_ids(self) -> list[str]:
        return [item["id"] for item in self.nodes]


def _safe_relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _node_id(text: str) -> str:
    hashed = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", text.replace("/", "_"))
    safe = safe.strip("._-")
    if not safe:
        safe = "node"
    return f"{safe}_{hashed}"[:120]


def _looks_like_code(path: Path) -> bool:
    return path.suffix.lower() in CODE_EXTENSIONS


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDE_DIRS for part in path.parts)


def _find_code_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if _is_excluded(p):
            continue
        if _looks_like_code(p):
            files.append(p)
    return sorted(files)


def _candidate_targets(files: list[Path]) -> dict[str, Path]:
    names = {}
    for file in files:
        rel = str(file)
        names[file.stem.lower()] = file
        names[rel.lower()] = file
    return names


def _extract_import_tokens(line: str, ext: str) -> list[str]:
    tokens: list[str] = []
    line = line.strip()
    if not line:
        return tokens

    patterns = []
    if ext == ".py":
        patterns = [
            re.compile(r"^from\s+([A-Za-z0-9_\.]+)\s+import\b"),
            re.compile(r"^import\s+([A-Za-z0-9_\.]+)") ,
        ]
    elif ext in {".js", ".jsx", ".ts", ".tsx"}:
        patterns = [
            re.compile(r"from\s+[\'\"]([^\'\"]+)[\'\"]"),
            re.compile(r"require\(\s*[\'\"]([^\'\"]+)[\'\"]\s*\)"),
            re.compile(r"import\(\s*[\'\"]([^\'\"]+)[\'\"]\s*\)"),
        ]
    elif ext == ".cs":
        patterns = [re.compile(r"^using\s+([A-Za-z0-9_\.]+)\s*;")]
    elif ext in {".java", ".kt", ".go", ".rust", ".swift"}:
        patterns = [re.compile(r"^import\s+([A-Za-z0-9_\.]+)\b")]

    for regex in patterns:
        m = regex.search(line)
        if m:
            tokens.append(m.group(1))

    return tokens

def _to_report_line_for_node_count(node_count: int, edge_count: int, community_count: int) -> str:
    return f"- Nodes: {node_count}, edges: {edge_count}, communities: {community_count}"


def _build_fallback_graph(root: Path, code_files: list[Path]) -> tuple[SimpleGraph, dict[int, list[str]], Counter[str], list[dict], dict[str, int]]:
    file_nodes: list[dict] = []
    links: list[dict] = []
    communities: dict[int, list[str]] = defaultdict(list)
    extension_hits = Counter()
    file_id_map: dict[Path, str] = {}

    for path in code_files:
        rel = path.relative_to(root)
        ext = path.suffix.lower()
        file_id = _node_id(str(rel))
        extension_hits[ext] += 1
        file_id_map[path] = file_id
        file_nodes.append(
            {
                "id": file_id,
                "label": path.name,
                "file_type": "code",
                "source_file": str(rel),
                "source_location": "L1",
                "_origin": "fallback",
                "community": -1,
            }
        )

    target_lookup = {name.lower(): pid for name, pid in [
        (str(path.relative_to(root)).lower(), fid) for path, fid in file_id_map.items()
    ]}
    for path, pid in file_id_map.items():
        try:
            target_lookup[path.stem.lower()] = pid
            target_lookup[path.name.lower()] = pid
        except Exception:
            continue

    for path in code_files:
        source = path.relative_to(root)
        source_id = file_id_map[path]
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                for token in _extract_import_tokens(line, path.suffix.lower()):
                    token_clean = token.strip().lstrip(".")
                    token_norm = token_clean.lower()
                    target = target_lookup.get(token_norm)
                    if target and target != source_id:
                        link = {
                            "source": source_id,
                            "target": target,
                            "relation": "imports",
                            "weight": 1.0,
                            "confidence": "FALLBACK",
                            "confidence_score": 1.0,
                            "source_file": str(source),
                            "source_location": "L1",
                        }
                        if link not in links:
                            links.append(link)
        except OSError:
            continue

    # Deterministic community assignment by extension.
    for idx, (ext, _) in enumerate(sorted(extension_hits.items(), key=lambda x: (-x[1], x[0]))):
        ext_file_ids = []
        for node in file_nodes:
            node_ext = Path(node["source_file"]).suffix.lower()
            if node_ext == ext:
                ext_file_ids.append(node["id"])
        if ext_file_ids:
            for node_id in ext_file_ids:
                communities[idx].append(node_id)
            for node in file_nodes:
                if node["id"] in ext_file_ids:
                    node["community"] = idx

    # If no files matched or all files had unknown extension, use single community.
    if not communities:
        communities = {0: [node["id"] for node in file_nodes]}
        for node in file_nodes:
            node["community"] = 0

    degree = Counter()
    for link in links:
        degree[link["source"]] += 1
        degree[link["target"]] += 1

    top_questions = []
    for node_id, count in sorted(degree.items(), key=lambda kv: kv[1], reverse=True)[:10]:
        node_label = next((item["label"] for item in file_nodes if item["id"] == node_id), node_id)
        top_questions.append(f"What is the dependency role of `{node_label}` ({count} links) ?")

    return SimpleGraph(nodes=file_nodes, links=links), communities, extension_hits, top_questions, degree


def _write_fallback_graph_json(out_dir: Path, graph: SimpleGraph, communities: dict[int, list[str]]) -> None:
    payload = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": graph.nodes,
        "links": graph.links,
        "hyperedges": [],
        "communities": {str(k): v for k, v in communities.items()},
        "built_at_commit": "",
    }
    (out_dir / "graph.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_fallback_manifest(out_dir: Path, detection: dict, repo_root: Path) -> None:
    manifest = {
        "repo_root": str(repo_root),
        "files": detection["files"],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _write_fallback_report(
    out_dir: Path,
    detection: dict,
    graph: SimpleGraph,
    communities: dict[int, list[str]],
    extension_hits: Counter[str],
    suggested_questions: list[str],
    repo_root: Path,
) -> None:
    report_lines = [
        "# Graph Report",
        "",
        f"Repository: {repo_root}",
        "",
        "## Summary",
        _to_report_line_for_node_count(graph.number_of_nodes(), graph.number_of_edges(), len(communities)),
        f"- Code files analyzed: {len(detection['files'].get('code', []))}",
        "",
        "## Top File Types",
    ]

    for ext, count in extension_hits.most_common():
        report_lines.append(f"- `{ext or '(no_ext)'}`: {count}")

    report_lines.extend(["", "## Communities", ""])
    for community_id, nodes in sorted(communities.items()):
        report_lines.append(f"### Community {community_id}")
        names = [n["label"] for n in graph.nodes if n["community"] == community_id][:20]
        report_lines.append(f"- Nodes: {len(nodes)}")
        report_lines.append(f"- Sample nodes: {', '.join(names) if names else 'N/A'}")
        report_lines.append("")

    if suggested_questions:
        report_lines.extend(["", "## Suggested Questions", ""])
        for q in suggested_questions[:20]:
            report_lines.append(f"- {q}")

    (out_dir / "GRAPH_REPORT.md").write_text("\n".join(report_lines), encoding="utf-8")


def _run_full_update(target: Path, max_code_files: int) -> tuple[
    dict,
    object,
    dict,
    list[dict],
    list,
    dict,
    dict,
    object,
]:
    detection = _graphify_detect(target)
    if "files" not in detection:
        detection = {"files": {"code": [], **(detection.get("files", {}))}}

    code_files = [Path(path) for path in detection["files"].get("code", [])]
    if len(code_files) > max_code_files:
        print(
            f"[graphify update] large codebase detected ({len(code_files)} files). "
            f"Running bounded AST-style update on first {max_code_files} files."
        )
        code_files = sorted(code_files)[:max_code_files]

    extraction = _graphify_extract(code_files)
    graph = _graphify_build_from_json(extraction)
    try:
        from graphify.cluster import cluster as _cluster, score_all as _score_all

        communities = _cluster(graph)
        cohesion = _score_all(graph, communities)
    except Exception as exc:
        print(f"[graphify update] cluster fallback: {exc}")
        communities, cohesion = {}, {}

    gods = _graphify_god_nodes(graph)
    surprises = _graphify_surprises(graph, communities)
    questions = _graphify_suggest_questions(graph, communities, {})

    return detection, graph, communities, gods, surprises, questions, extraction, cohesion


def _run_fallback_update(target: Path, repo_root: Path, max_code_files: int) -> tuple[dict, SimpleGraph, dict[int, list[str]], list[str], Counter[str], Counter[str]]:
    code_files = _find_code_files(target)
    print(f"[graphify update] graphify package unavailable ({_FULL_SUPPORT_ERROR.__class__.__name__}: {_FULL_SUPPORT_ERROR}); using fallback update mode")
    if len(code_files) > max_code_files:
        print(
            f"[graphify update] large codebase detected ({len(code_files)} files). "
            f"Running bounded fallback update on first {max_code_files} files."
        )
        code_files = sorted(code_files)[:max_code_files]

    detection = {"files": {"code": [str(p) for p in code_files], "other": []}}
    graph, communities, extension_hits, suggested_questions, degree = _build_fallback_graph(target, code_files)
    return detection, graph, communities, suggested_questions, degree, extension_hits


def run_update(target: Path, repo_root: Path | None = None, max_code_files: int = 1500) -> int:
    root = target.resolve()
    repo_root = repo_root.resolve() if repo_root else root

    if _FULL_SUPPORT:
        print("[graphify update] graphify package detected; attempting full update pipeline")
        detection, graph, communities, gods, surprises, questions, extraction, cohesion = _run_full_update(
            target=root,
            max_code_files=max_code_files,
        )
    else:
        detection, graph, communities, questions, degree, extension_hits = _run_fallback_update(
            target=root, repo_root=repo_root, max_code_files=max_code_files
        )
        out = root / "graphify-out"
        out.mkdir(parents=True, exist_ok=True)
        _write_fallback_graph_json(out, graph, communities)
        _write_fallback_manifest(out, detection, repo_root)
        _write_fallback_report(
            out,
            detection=detection,
            graph=graph,
            communities=communities,
            extension_hits=extension_hits,
            suggested_questions=questions,
            repo_root=repo_root,
        )
        print(
            f"[graphify update] graph updated: {graph.number_of_nodes()} nodes · "
            f"{graph.number_of_edges()} edges · {len(communities)} communities"
        )
        print(f"[graphify update] outputs written under {out}")
        return 0

    out = root / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)

    # The full pipeline expects these outputs from graphify package. Keep it unchanged when available.
    labels = {community_id: f"Community {community_id}" for community_id in communities}
    report = _graphify_generate(
        graph,
        communities,
        cohesion if "cohesion" in locals() else {},
        labels,
        gods,
        surprises,
        detection,
        {"input": extraction.get("input_tokens", 0), "output": extraction.get("output_tokens", 0)},
        str(root),
        suggested_questions=questions,
    )
    (out / "GRAPH_REPORT.md").write_text(report)
    _graphify_to_json(graph, communities, str(out / "graph.json"))

    try:
        _graphify_save_manifest(detection["files"], manifest_path=str(out / "manifest.json"))
    except Exception as exc:
        print(f"[graphify update] manifest write skipped: {exc}")

    print(
        f"[graphify update] graph updated: {graph.number_of_nodes()} nodes · "
        f"{graph.number_of_edges()} edges · {len(communities)} communities"
    )
    print(f"[graphify update] outputs written under {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    target = Path(args[0]) if len(args) > 0 else Path(".")
    repo_root = Path(args[1]) if len(args) > 1 else None
    max_code_files = int(args[2]) if len(args) > 2 else 1500
    return run_update(target, repo_root, max_code_files=max_code_files)


if __name__ == "__main__":
    raise SystemExit(main())
