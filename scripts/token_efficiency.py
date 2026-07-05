#!/usr/bin/env python3
"""Lightweight token-efficiency telemetry for Codex repo work.

Default mode is local and cheap: estimate plain-text tokens without calling any
API, write one immutable JSON event per run, and regenerate summaries from those
events after branch merges.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any


STORE_DEFAULT = Path(".codex/token-usage")
SCHEMA_VERSION = 1

API_RATES_PER_MTOK = {
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
}

CODEX_CREDITS_PER_MTOK = {
    "gpt-5.5": {"input": 125.0, "cached_input": 12.5, "output": 750.0},
    "gpt-5.4": {"input": 62.5, "cached_input": 6.25, "output": 375.0},
    "gpt-5.4-mini": {"input": 18.75, "cached_input": 1.875, "output": 113.0},
}

MODEL_ALIASES = {
    "frontier_deep": ("gpt-5.5", "xhigh"),
    "frontier_balanced": ("gpt-5.5", "medium"),
    "frontier_reviewer": ("gpt-5.5", "high"),
    "standard_balanced": ("gpt-5.4", "medium"),
    "light_worker": ("gpt-5.4-mini", "low"),
    "atomic_worker": ("gpt-5.4-nano", "none"),
    "realtime_editor": ("gpt-5.3-codex-spark", "low"),
}

REASONING_OUTPUT_RESERVE = {
    "none": 0.0,
    "minimal": 0.05,
    "low": 0.12,
    "medium": 0.35,
    "high": 0.75,
    "xhigh": 1.25,
    "max": 1.50,
}

PALETTE = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2"]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def run_git(args: list[str], default: str = "") -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return default
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else default


def repo_metadata() -> dict[str, str]:
    root = run_git(["rev-parse", "--show-toplevel"], default=str(Path.cwd()))
    return {
        "name": Path(root).name,
        "path": root,
        "branch": run_git(["branch", "--show-current"], default="unknown"),
        "commit": run_git(["rev-parse", "--short", "HEAD"], default="unknown"),
        "remote": run_git(["config", "--get", "remote.origin.url"], default=""),
    }


def safe_slug(value: str, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned[:80] or fallback


def parse_csv(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return out


def read_text_parts(text_values: list[str] | None, file_values: list[str] | None, use_stdin: bool) -> str:
    parts: list[str] = []
    for value in text_values or []:
        parts.append(value)
    for file_name in file_values or []:
        path = Path(file_name)
        try:
            parts.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError as exc:
            raise SystemExit(f"Unable to read {path}: {exc}") from exc
    if use_stdin and not sys.stdin.isatty():
        parts.append(sys.stdin.read())
    return "\n".join(part for part in parts if part)


def text_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def count_text_tokens(text: str, model: str) -> tuple[int, str, str]:
    if not text:
        return 0, "empty", "exact"
    try:
        import tiktoken  # type: ignore

        try:
            encoding = tiktoken.encoding_for_model(model)
        except Exception:
            encoding = tiktoken.get_encoding("o200k_base")
        return len(encoding.encode(text)), "tiktoken", "high"
    except Exception:
        compact_chars = len(text.encode("utf-8", errors="ignore"))
        words = len(re.findall(r"\S+", text))
        char_estimate = compact_chars / 4.0
        word_estimate = words * 1.3
        estimate = math.ceil(max(char_estimate, word_estimate, 1.0))
        return estimate, "chars_words_heuristic", "medium"


def normalize_model(model: str | None, alias: str | None, effort: str | None) -> tuple[str, str]:
    if alias:
        mapped = MODEL_ALIASES.get(alias)
        if not mapped:
            raise SystemExit(f"Unknown model alias: {alias}")
        mapped_model, mapped_effort = mapped
        return model or mapped_model, effort or mapped_effort
    return model or "gpt-5.5", effort or "medium"


def estimate_cost(tokens: dict[str, int], model: str, rates: dict[str, dict[str, float]]) -> float | None:
    rate = rates.get(model)
    if not rate:
        return None
    cached = min(tokens["cached_input_tokens"], tokens["input_tokens"])
    uncached = max(tokens["input_tokens"] - cached, 0)
    return (
        uncached / 1_000_000 * rate["input"]
        + cached / 1_000_000 * rate["cached_input"]
        + tokens["output_tokens"] / 1_000_000 * rate["output"]
    )


def estimate_usage(args: argparse.Namespace) -> dict[str, Any]:
    model, effort = normalize_model(args.model, args.alias, args.reasoning_effort)
    input_text = read_text_parts(args.input_text, args.input_text_file, args.stdin)
    output_text = read_text_parts(args.output_text, args.output_text_file, False)

    if args.input_tokens is None:
        input_tokens, input_method, input_confidence = count_text_tokens(input_text, model)
    else:
        input_tokens = max(args.input_tokens, 0)
        input_method = "provided"
        input_confidence = "exact_if_from_provider"

    cached_input_tokens = max(args.cached_input_tokens or 0, 0)

    if args.output_tokens is not None:
        visible_output_tokens = None
        reasoning_output_tokens = None
        output_tokens = max(args.output_tokens, 0)
        output_method = "provided_total"
        output_confidence = "exact_if_from_provider"
    else:
        if args.visible_output_tokens is None:
            visible_output_tokens, output_method, output_confidence = count_text_tokens(output_text, model)
        else:
            visible_output_tokens = max(args.visible_output_tokens, 0)
            output_method = "provided_visible"
            output_confidence = "medium"
        reserve = REASONING_OUTPUT_RESERVE.get(effort, REASONING_OUTPUT_RESERVE["medium"])
        reasoning_output_tokens = math.ceil(visible_output_tokens * reserve)
        output_tokens = visible_output_tokens + reasoning_output_tokens

    tokens = {
        "input_tokens": input_tokens,
        "cached_input_tokens": min(cached_input_tokens, input_tokens),
        "output_tokens": output_tokens,
        "visible_output_tokens": visible_output_tokens,
        "reasoning_output_tokens_estimated": reasoning_output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    api_cost = estimate_cost(tokens, model, API_RATES_PER_MTOK)
    codex_credits = estimate_cost(tokens, model, CODEX_CREDITS_PER_MTOK)
    return {
        "model": model,
        "reasoning_effort": effort,
        "tokens": tokens,
        "estimated_api_usd": api_cost,
        "estimated_codex_credits": codex_credits,
        "estimation": {
            "input_method": input_method,
            "input_confidence": input_confidence,
            "output_method": output_method,
            "output_confidence": output_confidence,
            "input_sha256": text_hash(input_text),
            "output_sha256": text_hash(output_text),
            "raw_text_stored": False,
            "note": "Use provider-reported usage when available; local counts are estimates for plain text.",
        },
    }


def event_path(store: Path, created_at: str, branch: str, event_id: str) -> Path:
    day = dt.datetime.fromisoformat(created_at).date()
    name = f"{created_at.replace(':', '').replace('+0000', 'Z')}_{safe_slug(branch)}_{event_id[:8]}.json"
    return store / "events" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}" / name


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(args: argparse.Namespace) -> int:
    store = Path(args.store)
    usage = estimate_usage(args)
    meta = repo_metadata()
    created = utc_now()
    event_id = str(uuid.uuid4())
    branch = args.branch or meta["branch"]
    event = {
        "schema_version": SCHEMA_VERSION,
        "id": event_id,
        "created_at_utc": created,
        "repo": {
            "name": args.repo or meta["name"],
            "path": meta["path"],
            "remote": meta["remote"],
        },
        "git": {
            "branch": branch,
            "commit": args.commit or meta["commit"],
        },
        "task": {
            "summary": args.task,
            "kind": args.kind,
            "tags": parse_csv(args.tag),
            "changes": parse_csv(args.change),
        },
        "routing": {
            "model": usage["model"],
            "reasoning_effort": usage["reasoning_effort"],
            "workers": parse_csv(args.worker),
        },
        "usage": usage,
        "process": {
            "graph_consulted": args.graph_consulted,
            "graph_refreshed": args.graph_refreshed,
            "validation": parse_csv(args.validation),
            "notes": args.notes or "",
        },
    }
    path = event_path(store, created, branch, event_id)
    if args.dry_run:
        print(json.dumps(event, indent=2, sort_keys=True))
        return 0
    write_json(path, event)
    if args.write_summary:
        write_summary(store, load_events(store))
    print(compact_event_line(event, path))
    return 0


def load_events(store: Path) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for path in sorted((store / "events").glob("**/*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipping {path}: {exc}", file=sys.stderr)
            continue
        event_id = str(event.get("id") or path)
        events[event_id] = event
    return sorted(events.values(), key=lambda e: (e.get("created_at_utc", ""), e.get("id", "")))


def nested_get(event: dict[str, Any], path: str, default: Any = None) -> Any:
    value: Any = event
    for part in path.split("."):
        if not isinstance(value, dict):
            return default
        value = value.get(part)
    return value if value is not None else default


def add_group(groups: dict[str, dict[str, Any]], key: str, event: dict[str, Any]) -> None:
    usage = nested_get(event, "usage.tokens", {})
    bucket = groups.setdefault(
        key,
        {
            "events": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_api_usd": 0.0,
            "estimated_codex_credits": 0.0,
        },
    )
    bucket["events"] += 1
    for name in ["input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"]:
        bucket[name] += int(usage.get(name) or 0)
    bucket["estimated_api_usd"] += float(nested_get(event, "usage.estimated_api_usd", 0.0) or 0.0)
    bucket["estimated_codex_credits"] += float(nested_get(event, "usage.estimated_codex_credits", 0.0) or 0.0)


def summarize(events: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "events": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_api_usd": 0.0,
        "estimated_codex_credits": 0.0,
    }
    by_branch: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_kind: dict[str, dict[str, Any]] = {}
    by_tag: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    for event in events:
        usage = nested_get(event, "usage.tokens", {})
        totals["events"] += 1
        for name in ["input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"]:
            totals[name] += int(usage.get(name) or 0)
        totals["estimated_api_usd"] += float(nested_get(event, "usage.estimated_api_usd", 0.0) or 0.0)
        totals["estimated_codex_credits"] += float(nested_get(event, "usage.estimated_codex_credits", 0.0) or 0.0)
        add_group(by_branch, nested_get(event, "git.branch", "unknown"), event)
        add_group(by_model, nested_get(event, "routing.model", "unknown"), event)
        add_group(by_kind, nested_get(event, "task.kind", "unspecified"), event)
        day = str(event.get("created_at_utc", ""))[:10] or "unknown"
        add_group(by_day, day, event)
        tags = nested_get(event, "task.tags", []) or ["untagged"]
        for tag in tags:
            add_group(by_tag, str(tag), event)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": utc_now(),
        "totals": rounded_money(totals),
        "by_branch": rounded_groups(by_branch),
        "by_model": rounded_groups(by_model),
        "by_kind": rounded_groups(by_kind),
        "by_tag": rounded_groups(by_tag),
        "by_day": rounded_groups(by_day),
    }


def rounded_money(bucket: dict[str, Any]) -> dict[str, Any]:
    out = dict(bucket)
    out["estimated_api_usd"] = round(float(out.get("estimated_api_usd", 0.0)), 6)
    out["estimated_codex_credits"] = round(float(out.get("estimated_codex_credits", 0.0)), 4)
    return out


def rounded_groups(groups: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {key: rounded_money(value) for key, value in sorted(groups.items())}


def write_summary(store: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    store.mkdir(parents=True, exist_ok=True)
    summary = summarize(events)
    write_json(store / "summary.json", summary)
    ledger = store / "ledger.jsonl"
    with ledger.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    return summary


def merge(args: argparse.Namespace) -> int:
    store = Path(args.store)
    events = load_events(store)
    summary = write_summary(store, events) if args.write_summary else summarize(events)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(summary_markdown(summary, compact=args.compact))
    return 0


def top_group(summary: dict[str, Any], group_name: str) -> tuple[str, dict[str, Any]] | None:
    groups = summary.get(group_name) or {}
    if not groups:
        return None
    return max(groups.items(), key=lambda item: item[1].get("total_tokens", 0))


def summary_markdown(summary: dict[str, Any], compact: bool = False) -> str:
    totals = summary.get("totals", {})
    branch = top_group(summary, "by_branch")
    model = top_group(summary, "by_model")
    tag = top_group(summary, "by_tag")
    lines = [
        "Token efficiency summary",
        f"- events: {totals.get('events', 0)}",
        f"- estimated total tokens: {totals.get('total_tokens', 0)}",
        f"- estimated API cost: ${float(totals.get('estimated_api_usd', 0.0)):.6f}",
        f"- estimated Codex credits: {float(totals.get('estimated_codex_credits', 0.0)):.4f}",
    ]
    if branch:
        lines.append(f"- top branch: {branch[0]} ({branch[1].get('total_tokens', 0)} tokens)")
    if model:
        lines.append(f"- top model: {model[0]} ({model[1].get('total_tokens', 0)} tokens)")
    if tag and not compact:
        lines.append(f"- top tag: {tag[0]} ({tag[1].get('total_tokens', 0)} tokens)")
    return "\n".join(lines)


def report(args: argparse.Namespace) -> int:
    store = Path(args.store)
    events = load_events(store)
    summary = summarize(events)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(summary_markdown(summary, compact=args.compact))
    return 0


def compact_event_line(event: dict[str, Any], path: Path) -> str:
    usage = nested_get(event, "usage.tokens", {})
    api_cost = nested_get(event, "usage.estimated_api_usd", None)
    credits = nested_get(event, "usage.estimated_codex_credits", None)
    cost_text = f"${api_cost:.6f}" if isinstance(api_cost, (int, float)) else "n/a"
    credit_text = f"{credits:.4f}" if isinstance(credits, (int, float)) else "n/a"
    return (
        f"recorded {path} | model={nested_get(event, 'routing.model', 'unknown')} "
        f"effort={nested_get(event, 'routing.reasoning_effort', 'unknown')} "
        f"tokens={usage.get('total_tokens', 0)} api={cost_text} codex_credits={credit_text}"
    )


def estimate(args: argparse.Namespace) -> int:
    usage = estimate_usage(args)
    if args.json:
        print(json.dumps(usage, indent=2, sort_keys=True))
    else:
        tokens = usage["tokens"]
        print(
            f"model={usage['model']} effort={usage['reasoning_effort']} "
            f"input={tokens['input_tokens']} cached={tokens['cached_input_tokens']} "
            f"output={tokens['output_tokens']} total={tokens['total_tokens']} "
            f"api_usd={usage['estimated_api_usd']} codex_credits={usage['estimated_codex_credits']}"
        )
    return 0


def chart_points(groups: dict[str, dict[str, Any]], limit: int = 12) -> list[tuple[str, float]]:
    items = [(key, float(value.get("total_tokens", 0))) for key, value in groups.items()]
    return sorted(items, key=lambda item: item[1], reverse=True)[:limit]


def svg_bar_chart(title: str, points: list[tuple[str, float]], width: int = 960, height: int = 520) -> str:
    margin_left = 190
    margin_right = 40
    margin_top = 74
    row_h = 34
    chart_w = width - margin_left - margin_right
    max_value = max([value for _, value in points] or [1.0])
    rows = []
    for i, (label, value) in enumerate(points):
        y = margin_top + i * row_h
        bar_w = 0 if max_value == 0 else int(chart_w * value / max_value)
        color = PALETTE[i % len(PALETTE)]
        rows.append(
            f'<text x="24" y="{y + 21}" class="label">{escape_xml(label[:28])}</text>'
            f'<rect x="{margin_left}" y="{y}" width="{bar_w}" height="22" rx="5" fill="{color}"/>'
            f'<text x="{margin_left + bar_w + 8}" y="{y + 17}" class="value">{int(value)}</text>'
        )
    body_h = max(height, margin_top + max(1, len(points)) * row_h + 42)
    return SVG_TEMPLATE.format(width=width, height=body_h, title=escape_xml(title), body="\n".join(rows))


def svg_line_chart(title: str, points: list[tuple[str, float]], width: int = 960, height: int = 420) -> str:
    if not points:
        points = [("none", 0.0)]
    margin_left = 66
    margin_right = 36
    margin_top = 74
    margin_bottom = 76
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom
    max_value = max([value for _, value in points] or [1.0]) or 1.0
    coords: list[tuple[float, float]] = []
    for i, (_, value) in enumerate(points):
        x = margin_left + (chart_w * i / max(1, len(points) - 1))
        y = margin_top + chart_h - (chart_h * value / max_value)
        coords.append((x, y))
    path = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords))
    dots = []
    labels = []
    for i, ((label, value), (x, y)) in enumerate(zip(points, coords)):
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{PALETTE[i % len(PALETTE)]}"/>')
        if i == 0 or i == len(points) - 1 or len(points) <= 8:
            labels.append(f'<text x="{x:.1f}" y="{height - 28}" class="axis" text-anchor="middle">{escape_xml(label[-5:])}</text>')
            labels.append(f'<text x="{x:.1f}" y="{y - 12:.1f}" class="value" text-anchor="middle">{int(value)}</text>')
    body = (
        f'<line x1="{margin_left}" y1="{margin_top + chart_h}" x2="{width - margin_right}" y2="{margin_top + chart_h}" class="grid"/>'
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{margin_top + chart_h}" class="grid"/>'
        f'<path d="{path}" fill="none" stroke="#2563eb" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
        + "\n".join(dots + labels)
    )
    return SVG_TEMPLATE.format(width=width, height=height, title=escape_xml(title), body=body)


SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<style>
  .bg {{ fill: #f8fafc; }}
  .title {{ fill: #0f172a; font: 700 28px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .label {{ fill: #334155; font: 600 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .value {{ fill: #0f172a; font: 600 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .axis {{ fill: #64748b; font: 500 12px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  .grid {{ stroke: #cbd5e1; stroke-width: 1; }}
</style>
<rect class="bg" width="{width}" height="{height}" rx="0"/>
<text x="24" y="44" class="title">{title}</text>
{body}
</svg>
"""


def escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def write_matplotlib_charts(summary: dict[str, Any], out_dir: Path) -> list[str]:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
    except Exception:
        return []

    written: list[str] = []
    charts = {
        "by_branch": chart_points(summary.get("by_branch", {})),
        "by_model": chart_points(summary.get("by_model", {})),
        "by_tag": chart_points(summary.get("by_tag", {})),
    }
    for name, points in charts.items():
        if not points:
            continue
        labels = [p[0] for p in points]
        values = [p[1] for p in points]
        fig, ax = plt.subplots(figsize=(11, 6), facecolor="#f8fafc")
        ax.set_facecolor("#f8fafc")
        ax.barh(labels, values, color=PALETTE[: len(values)])
        ax.invert_yaxis()
        ax.set_title(f"Token efficiency {name.replace('_', ' ')}", loc="left", fontweight="bold")
        ax.set_xlabel("Estimated total tokens")
        ax.grid(axis="x", color="#cbd5e1", alpha=0.7)
        for spine in ax.spines.values():
            spine.set_visible(False)
        path = out_dir / f"token-efficiency-{name}.png"
        fig.tight_layout()
        fig.savefig(path, dpi=160)
        plt.close(fig)
        written.append(path.name)

    day_points = sorted((summary.get("by_day") or {}).items())
    if day_points:
        labels = [item[0] for item in day_points]
        values = [float(item[1].get("total_tokens", 0)) for item in day_points]
        fig, ax = plt.subplots(figsize=(11, 5), facecolor="#f8fafc")
        ax.set_facecolor("#f8fafc")
        ax.plot(labels, values, color="#2563eb", linewidth=3, marker="o")
        ax.set_title("Token efficiency timeline", loc="left", fontweight="bold")
        ax.set_ylabel("Estimated total tokens")
        ax.grid(axis="y", color="#cbd5e1", alpha=0.7)
        for label in ax.get_xticklabels():
            label.set_rotation(35)
            label.set_ha("right")
        for spine in ax.spines.values():
            spine.set_visible(False)
        path = out_dir / "token-efficiency-timeline.png"
        fig.tight_layout()
        fig.savefig(path, dpi=160)
        plt.close(fig)
        written.append(path.name)
    return written


def visualize(args: argparse.Namespace) -> int:
    store = Path(args.store)
    out_dir = Path(args.output_dir) if args.output_dir else store / "visuals"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(load_events(store))
    write_json(out_dir / "remotion-token-efficiency-data.json", summary)

    assets = write_matplotlib_charts(summary, out_dir)
    if not assets:
        svg_specs = {
            "token-efficiency-timeline.svg": svg_line_chart(
                "Token efficiency timeline",
                sorted((key, value["total_tokens"]) for key, value in (summary.get("by_day") or {}).items()),
            ),
            "token-efficiency-by-branch.svg": svg_bar_chart(
                "Token efficiency by branch", chart_points(summary.get("by_branch", {}))
            ),
            "token-efficiency-by-model.svg": svg_bar_chart(
                "Token efficiency by model", chart_points(summary.get("by_model", {}))
            ),
            "token-efficiency-by-tag.svg": svg_bar_chart(
                "Token efficiency by tag", chart_points(summary.get("by_tag", {}))
            ),
        }
        for name, svg in svg_specs.items():
            (out_dir / name).write_text(svg, encoding="utf-8")
            assets.append(name)

    index = html_report(summary, assets)
    (out_dir / "index.html").write_text(index, encoding="utf-8")
    remotion_dir = Path(args.remotion_dir) if args.remotion_dir else out_dir / "remotion"
    if not args.skip_remotion:
        write_remotion_package(summary, remotion_dir)
    print(f"wrote {out_dir / 'index.html'}")
    for asset in assets:
        print(f"wrote {out_dir / asset}")
    print(f"wrote {out_dir / 'remotion-token-efficiency-data.json'}")
    if not args.skip_remotion:
        print(f"wrote {remotion_dir / 'package.json'}")
        print(f"preview with: cd {remotion_dir} && npm install && npm run studio")
        print(f"render still: cd {remotion_dir} && npm install && npm run still")
    return 0


def write_remotion_package(summary: dict[str, Any], remotion_dir: Path) -> None:
    src_dir = remotion_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    write_json(src_dir / "token-usage-data.json", remotion_payload(summary))
    (remotion_dir / "package.json").write_text(REMOTION_PACKAGE_JSON, encoding="utf-8")
    (remotion_dir / "tsconfig.json").write_text(REMOTION_TSCONFIG_JSON, encoding="utf-8")
    (remotion_dir / "README.md").write_text(REMOTION_README_MD, encoding="utf-8")
    (src_dir / "index.ts").write_text(REMOTION_INDEX_TS, encoding="utf-8")
    (src_dir / "Root.tsx").write_text(REMOTION_ROOT_TSX, encoding="utf-8")
    (src_dir / "TokenUsageVideo.tsx").write_text(REMOTION_VIDEO_TSX, encoding="utf-8")


def top_items(groups: dict[str, dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    points = chart_points(groups, limit=limit)
    return [
        {
            "label": label,
            "value": int(value),
        }
        for label, value in points
    ]


def day_items(groups: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "label": label,
            "value": int(value.get("total_tokens", 0)),
        }
        for label, value in sorted(groups.items())
    ]


def remotion_payload(summary: dict[str, Any]) -> dict[str, Any]:
    totals = summary.get("totals", {})
    return {
        "title": "Token Efficiency Report",
        "generatedAtUtc": summary.get("generated_at_utc"),
        "totals": {
            "events": int(totals.get("events", 0)),
            "totalTokens": int(totals.get("total_tokens", 0)),
            "inputTokens": int(totals.get("input_tokens", 0)),
            "cachedInputTokens": int(totals.get("cached_input_tokens", 0)),
            "outputTokens": int(totals.get("output_tokens", 0)),
            "estimatedApiUsd": float(totals.get("estimated_api_usd", 0.0)),
            "estimatedCodexCredits": float(totals.get("estimated_codex_credits", 0.0)),
        },
        "sheets": [
            {
                "key": "overview",
                "title": "Overview",
                "kind": "overview",
                "items": [],
            },
            {
                "key": "timeline",
                "title": "Timeline",
                "kind": "line",
                "items": day_items(summary.get("by_day", {})),
            },
            {
                "key": "branches",
                "title": "Usage by Branch",
                "kind": "bar",
                "items": top_items(summary.get("by_branch", {})),
            },
            {
                "key": "models",
                "title": "Usage by Model",
                "kind": "bar",
                "items": top_items(summary.get("by_model", {})),
            },
            {
                "key": "task-kinds",
                "title": "Usage by Task Kind",
                "kind": "bar",
                "items": top_items(summary.get("by_kind", {})),
            },
            {
                "key": "tags",
                "title": "Usage by Tag",
                "kind": "bar",
                "items": top_items(summary.get("by_tag", {})),
            },
        ],
    }


REMOTION_PACKAGE_JSON = """{
  "name": "token-efficiency-remotion-report",
  "private": true,
  "scripts": {
    "studio": "remotion studio src/index.ts",
    "render": "remotion render src/index.ts TokenUsageReport out/token-usage-report.mp4",
    "still": "remotion still src/index.ts TokenUsageReport out/token-usage-report.png --frame=30"
  },
  "dependencies": {
    "@remotion/cli": "latest",
    "remotion": "latest",
    "react": "latest",
    "react-dom": "latest"
  },
  "devDependencies": {
    "@types/react": "latest",
    "@types/react-dom": "latest",
    "typescript": "latest"
  }
}
"""


REMOTION_TSCONFIG_JSON = """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "resolveJsonModule": true,
    "skipLibCheck": true,
    "noEmit": true
  },
  "include": ["src"]
}
"""


REMOTION_README_MD = """# Token Efficiency Remotion Report

Generated by `python3 scripts/token_efficiency.py visualize`.

Commands:

```bash
npm install
npm run studio
npm run still
npm run render
```

The report is split into animated sheets: overview, timeline, branch, model, task kind, and tag. Animations are driven by Remotion frame hooks rather than CSS animations.
"""


REMOTION_INDEX_TS = """import { registerRoot } from "remotion";
import { RemotionRoot } from "./Root";

registerRoot(RemotionRoot);
"""


REMOTION_ROOT_TSX = """import { Composition } from "remotion";
import usageData from "./token-usage-data.json";
import { TokenUsageVideo, type TokenUsageVideoProps } from "./TokenUsageVideo";

export const RemotionRoot = () => {
  return (
    <Composition
      id="TokenUsageReport"
      component={TokenUsageVideo}
      durationInFrames={720}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={{ data: usageData } satisfies TokenUsageVideoProps}
    />
  );
};
"""


REMOTION_VIDEO_TSX = """import React from "react";
import {
  AbsoluteFill,
  Easing,
  Sequence,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

type ChartItem = {
  label: string;
  value: number;
};

type Sheet = {
  key: string;
  title: string;
  kind: "overview" | "bar" | "line";
  items: ChartItem[];
};

type TokenUsageData = {
  title: string;
  generatedAtUtc?: string;
  totals: {
    events: number;
    totalTokens: number;
    inputTokens: number;
    cachedInputTokens: number;
    outputTokens: number;
    estimatedApiUsd: number;
    estimatedCodexCredits: number;
  };
  sheets: Sheet[];
};

export type TokenUsageVideoProps = {
  data: TokenUsageData;
};

const SHEET_DURATION_SECONDS = 4;
const palette = ["#2563eb", "#059669", "#d97706", "#7c3aed", "#dc2626", "#0891b2"];

const formatNumber = (value: number) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 }).format(value);

const formatDecimal = (value: number) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(value);

const formatMoney = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 4,
  }).format(value);

const clampLabel = (label: string) => (label.length > 30 ? `${label.slice(0, 27)}...` : label);

const cardStyle: React.CSSProperties = {
  background: "#f8fafc",
  border: "1px solid #cbd5e1",
  borderRadius: 8,
  boxShadow: "0 18px 42px rgba(15, 23, 42, 0.08)",
};

const MetricCard: React.FC<{ label: string; value: string; delay: number; localFrame: number; fps: number }> = ({
  label,
  value,
  delay,
  localFrame,
  fps,
}) => {
  const scale = spring({
    frame: localFrame - delay,
    fps,
    config: { damping: 180, stiffness: 120 },
  });
  const opacity = interpolate(localFrame, [delay, delay + 16], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return (
    <div
      style={{
        ...cardStyle,
        padding: 30,
        opacity,
        transform: `translateY(${(1 - scale) * 24}px)`,
      }}
    >
      <div style={{ color: "#64748b", fontSize: 24, fontWeight: 700 }}>{label}</div>
      <div style={{ color: "#0f172a", fontSize: 58, fontWeight: 800, marginTop: 12 }}>{value}</div>
    </div>
  );
};

const OverviewSheet: React.FC<{ data: TokenUsageData; localFrame: number; fps: number }> = ({
  data,
  localFrame,
  fps,
}) => {
  const metrics = [
    ["Events", formatNumber(data.totals.events)],
    ["Total Tokens", formatNumber(data.totals.totalTokens)],
    ["API Estimate", formatMoney(data.totals.estimatedApiUsd)],
    ["Codex Credits", formatDecimal(data.totals.estimatedCodexCredits)],
  ];

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 28, marginTop: 48 }}>
      {metrics.map(([label, value], index) => (
        <MetricCard
          key={label}
          label={label}
          value={value}
          delay={index * 6}
          localFrame={localFrame}
          fps={fps}
        />
      ))}
    </div>
  );
};

const BarSheet: React.FC<{ sheet: Sheet; localFrame: number; fps: number }> = ({ sheet, localFrame, fps }) => {
  const maxValue = Math.max(...sheet.items.map((item) => item.value), 1);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18, marginTop: 32 }}>
      {sheet.items.map((item, index) => {
        const progress = spring({
          frame: localFrame - index * 4,
          fps,
          config: { damping: 200, stiffness: 120 },
        });
        const width = `${Math.max(2, (item.value / maxValue) * 100 * progress)}%`;
        const opacity = interpolate(localFrame, [index * 4, index * 4 + 14], [0, 1], {
          extrapolateLeft: "clamp",
          extrapolateRight: "clamp",
          easing: Easing.bezier(0.16, 1, 0.3, 1),
        });

        return (
          <div key={`${sheet.key}-${item.label}`} style={{ opacity }}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "290px 1fr 170px",
                alignItems: "center",
                gap: 20,
              }}
            >
              <div style={{ color: "#334155", fontSize: 24, fontWeight: 700 }}>{clampLabel(item.label)}</div>
              <div style={{ height: 34, background: "#e2e8f0", borderRadius: 7, overflow: "hidden" }}>
                <div
                  style={{
                    width,
                    height: "100%",
                    background: palette[index % palette.length],
                    borderRadius: 7,
                  }}
                />
              </div>
              <div style={{ color: "#0f172a", fontSize: 24, fontWeight: 800, textAlign: "right" }}>
                {formatNumber(item.value)}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
};

const LineSheet: React.FC<{ sheet: Sheet; localFrame: number }> = ({ sheet, localFrame }) => {
  const width = 1380;
  const height = 520;
  const points = sheet.items.length > 0 ? sheet.items : [{ label: "none", value: 0 }];
  const maxValue = Math.max(...points.map((point) => point.value), 1);
  const coords = points.map((point, index) => {
    const x = points.length === 1 ? width / 2 : (index / (points.length - 1)) * width;
    const y = height - (point.value / maxValue) * height;
    return { ...point, x, y };
  });
  const reveal = interpolate(localFrame, [0, 80], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });
  const path = coords.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
  const visibleWidth = Math.max(0, width * reveal);

  return (
    <div style={{ ...cardStyle, padding: 38, marginTop: 38 }}>
      <svg width={width} height={height + 86} viewBox={`0 0 ${width} ${height + 86}`}>
        <defs>
          <clipPath id="line-reveal">
            <rect x={0} y={0} width={visibleWidth} height={height + 20} />
          </clipPath>
        </defs>
        <line x1={0} y1={height} x2={width} y2={height} stroke="#cbd5e1" strokeWidth={2} />
        <line x1={0} y1={0} x2={0} y2={height} stroke="#cbd5e1" strokeWidth={2} />
        <path
          d={path}
          fill="none"
          stroke="#2563eb"
          strokeWidth={6}
          strokeLinecap="round"
          strokeLinejoin="round"
          clipPath="url(#line-reveal)"
        />
        {coords.map((point, index) => (
          <g key={`${point.label}-${index}`} opacity={localFrame > index * 5 ? 1 : 0}>
            <circle cx={point.x} cy={point.y} r={8} fill={palette[index % palette.length]} />
            <text x={point.x} y={point.y - 18} textAnchor="middle" fill="#0f172a" fontSize={20} fontWeight={800}>
              {formatNumber(point.value)}
            </text>
            <text x={point.x} y={height + 48} textAnchor="middle" fill="#64748b" fontSize={18} fontWeight={700}>
              {point.label.slice(-5)}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
};

const SheetScene: React.FC<{ sheet: Sheet; data: TokenUsageData; sheetIndex: number }> = ({
  sheet,
  data,
  sheetIndex,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const localFrame = frame - sheetIndex * SHEET_DURATION_SECONDS * fps;
  const opacity = interpolate(localFrame, [0, 12, SHEET_DURATION_SECONDS * fps - 18, SHEET_DURATION_SECONDS * fps], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const lift = interpolate(localFrame, [0, 24], [24, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.bezier(0.16, 1, 0.3, 1),
  });

  return (
    <AbsoluteFill
      style={{
        opacity,
        padding: 74,
        background: "#e2e8f0",
        transform: `translateY(${lift}px)`,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <div style={{ color: "#64748b", fontSize: 24, fontWeight: 800, textTransform: "uppercase" }}>
            Sheet {sheetIndex + 1} / {data.sheets.length}
          </div>
          <h1 style={{ margin: "8px 0 0", color: "#0f172a", fontSize: 68, letterSpacing: 0, lineHeight: 1 }}>
            {sheet.title}
          </h1>
        </div>
        <div style={{ color: "#475569", fontSize: 23, fontWeight: 700, textAlign: "right" }}>
          <div>{data.title}</div>
          <div>{data.generatedAtUtc ?? "local summary"}</div>
        </div>
      </div>
      {sheet.kind === "overview" ? (
        <OverviewSheet data={data} localFrame={localFrame} fps={fps} />
      ) : sheet.kind === "line" ? (
        <LineSheet sheet={sheet} localFrame={localFrame} />
      ) : (
        <BarSheet sheet={sheet} localFrame={localFrame} fps={fps} />
      )}
    </AbsoluteFill>
  );
};

export const TokenUsageVideo: React.FC<TokenUsageVideoProps> = ({ data }) => {
  const { fps } = useVideoConfig();
  const sheetDuration = SHEET_DURATION_SECONDS * fps;

  return (
    <AbsoluteFill style={{ background: "#e2e8f0" }}>
      {data.sheets.map((sheet, index) => (
        <Sequence key={sheet.key} from={index * sheetDuration} durationInFrames={sheetDuration}>
          <SheetScene sheet={sheet} data={data} sheetIndex={index} />
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
"""


def html_report(summary: dict[str, Any], assets: list[str]) -> str:
    cards = "\n".join(
        f'<section class="chart"><h2>{escape_xml(asset)}</h2><img src="{escape_xml(asset)}" alt="{escape_xml(asset)}"></section>'
        for asset in assets
    )
    totals = summary.get("totals", {})
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Token Efficiency Report</title>
  <style>
    body {{ margin: 0; background: #e2e8f0; color: #0f172a; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 32px 20px 56px; }}
    header {{ margin-bottom: 22px; }}
    h1 {{ margin: 0 0 8px; font-size: 34px; letter-spacing: 0; }}
    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 18px 0 28px; }}
    .stat {{ background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 14px 16px; }}
    .stat strong {{ display: block; font-size: 24px; }}
    .chart {{ background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 8px; padding: 18px; margin: 16px 0; }}
    .chart img {{ display: block; width: 100%; height: auto; }}
    h2 {{ font-size: 15px; color: #475569; margin: 0 0 12px; }}
  </style>
</head>
<body>
<main>
  <header>
    <h1>Token Efficiency Report</h1>
    <p>Generated from branch-safe Codex token telemetry events.</p>
  </header>
  <section class="stats">
    <div class="stat"><span>Events</span><strong>{totals.get('events', 0)}</strong></div>
    <div class="stat"><span>Total tokens</span><strong>{totals.get('total_tokens', 0)}</strong></div>
    <div class="stat"><span>API estimate</span><strong>${float(totals.get('estimated_api_usd', 0.0)):.4f}</strong></div>
    <div class="stat"><span>Codex credits</span><strong>{float(totals.get('estimated_codex_credits', 0.0)):.2f}</strong></div>
  </section>
  {cards}
</main>
</body>
</html>
"""


def add_estimate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", help="Model id, e.g. gpt-5.5")
    parser.add_argument("--alias", choices=sorted(MODEL_ALIASES), help="Skill alias such as light_worker")
    parser.add_argument("--reasoning-effort", choices=sorted(REASONING_OUTPUT_RESERVE), help="Reasoning effort")
    parser.add_argument("--input-text", action="append", help="Input/prompt text to estimate")
    parser.add_argument("--input-text-file", action="append", help="File containing input/prompt text")
    parser.add_argument("--stdin", action="store_true", help="Read input text from stdin")
    parser.add_argument("--output-text", action="append", help="Visible output text to estimate")
    parser.add_argument("--output-text-file", action="append", help="File containing visible output text")
    parser.add_argument("--input-tokens", type=int, help="Provider-reported input tokens")
    parser.add_argument("--cached-input-tokens", type=int, default=0, help="Provider-reported cached input tokens")
    parser.add_argument("--visible-output-tokens", type=int, help="Estimated visible output tokens")
    parser.add_argument("--output-tokens", type=int, help="Provider-reported total output tokens")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Track lightweight Codex token-efficiency telemetry.")
    parser.add_argument("--store", default=str(STORE_DEFAULT), help="Telemetry store directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_estimate = sub.add_parser("estimate", help="Estimate tokens/cost without writing an event")
    add_estimate_args(p_estimate)
    p_estimate.add_argument("--json", action="store_true")
    p_estimate.set_defaults(func=estimate)

    p_record = sub.add_parser("record", help="Write one branch-safe usage event")
    add_estimate_args(p_record)
    p_record.add_argument("--task", required=True, help="Short task summary; do not paste secrets")
    p_record.add_argument("--kind", default="unspecified", help="Task kind, e.g. bugfix, audit, skill-update")
    p_record.add_argument("--tag", action="append", help="Comma-separated tags")
    p_record.add_argument("--change", action="append", help="Comma-separated change labels")
    p_record.add_argument("--worker", action="append", help="Comma-separated workers used, e.g. light_worker:2")
    p_record.add_argument("--graph-consulted", choices=["yes", "no", "n/a"], default="n/a")
    p_record.add_argument("--graph-refreshed", choices=["yes", "no", "n/a"], default="n/a")
    p_record.add_argument("--validation", action="append", help="Comma-separated validation steps")
    p_record.add_argument("--notes", help="Short notes; do not store secrets")
    p_record.add_argument("--repo", help="Override repo name")
    p_record.add_argument("--branch", help="Override branch")
    p_record.add_argument("--commit", help="Override commit")
    p_record.add_argument("--write-summary", action="store_true", help="Also regenerate summary.json and ledger.jsonl")
    p_record.add_argument("--dry-run", action="store_true")
    p_record.set_defaults(func=record)

    p_merge = sub.add_parser("merge", help="Deduplicate events and regenerate summaries after branch merges")
    p_merge.add_argument("--write-summary", action="store_true", help="Write summary.json and ledger.jsonl")
    p_merge.add_argument("--compact", action="store_true")
    p_merge.add_argument("--json", action="store_true")
    p_merge.set_defaults(func=merge)

    p_report = sub.add_parser("report", help="Print a lightweight usage report")
    p_report.add_argument("--compact", action="store_true")
    p_report.add_argument("--json", action="store_true")
    p_report.set_defaults(func=report)

    p_visualize = sub.add_parser("visualize", help="Write visual usage report assets")
    p_visualize.add_argument("--output-dir", help="Output directory; defaults to .codex/token-usage/visuals")
    p_visualize.add_argument("--remotion-dir", help="Remotion package output; defaults to <output-dir>/remotion")
    p_visualize.add_argument("--skip-remotion", action="store_true", help="Skip writing the Remotion package")
    p_visualize.set_defaults(func=visualize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
