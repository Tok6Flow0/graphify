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
import shutil
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


def parse_event_time(event: dict[str, Any]) -> dt.datetime | None:
    raw = str(event.get("created_at_utc") or "")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_total_tokens(event: dict[str, Any]) -> int:
    return int(nested_get(event, "usage.tokens.total_tokens", 0) or 0)


def event_output_tokens(event: dict[str, Any]) -> int:
    return int(nested_get(event, "usage.tokens.output_tokens", 0) or 0)


def event_input_tokens(event: dict[str, Any]) -> int:
    return int(nested_get(event, "usage.tokens.input_tokens", 0) or 0)


def compact_number(value: float | int) -> str:
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{sign}{value / 1_000:.1f}k"
    if value == int(value):
        return f"{sign}{int(value)}"
    return f"{sign}{value:.1f}"


def money(value: float | int) -> str:
    return f"${float(value):,.4f}"


def pct(value: float | int) -> str:
    return f"{float(value) * 100:.1f}%"


def display_date(value: dt.date | str) -> str:
    if isinstance(value, str):
        try:
            value = dt.date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value.strftime("%b %-d") if os.name != "nt" else value.strftime("%b %#d")


def display_time(value: dt.datetime | None) -> str:
    if value is None:
        return "unknown"
    local = value.astimezone()
    return local.strftime("%b %-d, %-I:%M %p") if os.name != "nt" else local.strftime("%b %#d, %#I:%M %p")


def usage_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, event in enumerate(events, start=1):
        created = parse_event_time(event)
        tags = nested_get(event, "task.tags", []) or []
        workers = nested_get(event, "routing.workers", []) or []
        rows.append(
            {
                "index": index,
                "created": created,
                "created_label": display_time(created),
                "day": created.date().isoformat() if created else "unknown",
                "time_label": created.astimezone().strftime("%H:%M") if created else f"#{index}",
                "branch": str(nested_get(event, "git.branch", "unknown")),
                "kind": str(nested_get(event, "task.kind", "unspecified")),
                "summary": str(nested_get(event, "task.summary", "")),
                "tags": ", ".join(str(tag) for tag in tags) or "untagged",
                "model": str(nested_get(event, "routing.model", "unknown")),
                "effort": str(nested_get(event, "routing.reasoning_effort", "unknown")),
                "workers": ", ".join(str(worker) for worker in workers) or "none",
                "input_tokens": event_input_tokens(event),
                "output_tokens": event_output_tokens(event),
                "total_tokens": event_total_tokens(event),
                "api_usd": float(nested_get(event, "usage.estimated_api_usd", 0.0) or 0.0),
                "codex_credits": float(nested_get(event, "usage.estimated_codex_credits", 0.0) or 0.0),
            }
        )
    return rows


def group_rows(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = str(row.get(field) or "unknown")
        labels = [label.strip() for label in raw.split(",") if label.strip()] if field == "tags" else [raw]
        for label in labels or ["unknown"]:
            bucket = groups.setdefault(label, {"label": label, "events": 0, "total_tokens": 0, "api_usd": 0.0})
            bucket["events"] += 1
            bucket["total_tokens"] += int(row["total_tokens"])
            bucket["api_usd"] += float(row["api_usd"])
    out: list[dict[str, Any]] = []
    for bucket in groups.values():
        events = max(int(bucket["events"]), 1)
        bucket["avg_tokens"] = round(float(bucket["total_tokens"]) / events, 1)
        out.append(bucket)
    return sorted(out, key=lambda item: (item["total_tokens"], item["events"]), reverse=True)


def last_n_days(rows: list[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    dated = [row["created"].date() for row in rows if row.get("created")]
    end = max(dated) if dated else dt.datetime.now().astimezone().date()
    start = end - dt.timedelta(days=days - 1)
    buckets: dict[str, dict[str, Any]] = {}
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        buckets[day.isoformat()] = {
            "day": day.isoformat(),
            "label": display_date(day),
            "events": 0,
            "total_tokens": 0,
            "avg_tokens": 0.0,
            "api_usd": 0.0,
        }
    for row in rows:
        day = row.get("day")
        if day in buckets:
            buckets[day]["events"] += 1
            buckets[day]["total_tokens"] += int(row["total_tokens"])
            buckets[day]["api_usd"] += float(row["api_usd"])
    for bucket in buckets.values():
        if bucket["events"]:
            bucket["avg_tokens"] = round(float(bucket["total_tokens"]) / float(bucket["events"]), 1)
    return list(buckets.values())


def dashboard_metrics(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = summary.get("totals", {})
    events = max(int(totals.get("events", 0) or 0), 0)
    total_tokens = int(totals.get("total_tokens", 0) or 0)
    input_tokens = int(totals.get("input_tokens", 0) or 0)
    cached_tokens = int(totals.get("cached_input_tokens", 0) or 0)
    output_tokens = int(totals.get("output_tokens", 0) or 0)
    last7 = last_n_days(rows, 7)
    top_prompt = max(rows, key=lambda row: row["total_tokens"], default=None)
    worker_events = sum(1 for row in rows if row.get("workers") and row["workers"] != "none")
    return {
        "events": events,
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "cache_rate": (cached_tokens / input_tokens) if input_tokens else 0.0,
        "avg_tokens": round(total_tokens / events, 1) if events else 0.0,
        "last7_tokens": sum(int(day["total_tokens"]) for day in last7),
        "last7_events": sum(int(day["events"]) for day in last7),
        "last7_avg": round(
            sum(int(day["total_tokens"]) for day in last7) / max(sum(int(day["events"]) for day in last7), 1),
            1,
        ),
        "api_usd": float(totals.get("estimated_api_usd", 0.0) or 0.0),
        "codex_credits": float(totals.get("estimated_codex_credits", 0.0) or 0.0),
        "top_prompt_tokens": int(top_prompt["total_tokens"]) if top_prompt else 0,
        "top_prompt_label": top_prompt["created_label"] if top_prompt else "n/a",
        "worker_events": worker_events,
        "worker_share": worker_events / events if events else 0.0,
    }


def svg_prompt_sequence(rows: list[dict[str, Any]], width: int = 1180, height: int = 310) -> str:
    if not rows:
        return svg_empty("Prompt-level usage", "No token events have been recorded yet.", width, height)
    margin_left = 62
    margin_right = 24
    margin_top = 48
    margin_bottom = 72
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom
    max_value = max([int(row["total_tokens"]) for row in rows] or [1]) or 1
    coords: list[tuple[dict[str, Any], float, float]] = []
    for i, row in enumerate(rows):
        x = margin_left + (chart_w * i / max(1, len(rows) - 1)) if len(rows) > 1 else margin_left + chart_w / 2
        y = margin_top + chart_h - (chart_h * int(row["total_tokens"]) / max_value)
        coords.append((row, x, y))
    path = " ".join(("M" if i == 0 else "L") + f"{x:.1f},{y:.1f}" for i, (_, x, y) in enumerate(coords))
    dots: list[str] = []
    for i, (row, x, y) in enumerate(coords):
        color = PALETTE[i % len(PALETTE)]
        radius = 6 if len(rows) > 1 else 10
        dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}"/>')
        if len(rows) <= 14 or i in {0, len(rows) - 1}:
            dots.append(
                f'<text x="{x:.1f}" y="{height - 34}" class="axis" text-anchor="middle">'
                f'{escape_xml(str(row["time_label"]))}</text>'
            )
            dots.append(
                f'<text x="{x:.1f}" y="{max(20, y - 12):.1f}" class="value" text-anchor="middle">'
                f'{escape_xml(compact_number(row["total_tokens"]))}</text>'
            )
    body = (
        f'<line x1="{margin_left}" y1="{margin_top + chart_h}" x2="{width - margin_right}" y2="{margin_top + chart_h}" class="grid"/>'
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{margin_top + chart_h}" class="grid"/>'
        f'<path d="{path}" fill="none" stroke="#2563eb" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
        + "\n".join(dots)
    )
    return SVG_TEMPLATE.format(width=width, height=height, title="Prompt-level usage", body=body)


def svg_daily_rollup(days: list[dict[str, Any]], width: int = 1180, height: int = 330) -> str:
    if not days:
        return svg_empty("Last 7 days", "No day-level data available.", width, height)
    margin_left = 58
    margin_right = 22
    margin_top = 52
    margin_bottom = 78
    chart_w = width - margin_left - margin_right
    chart_h = height - margin_top - margin_bottom
    max_value = max([int(day["total_tokens"]) for day in days] or [1]) or 1
    bar_gap = 14
    bar_w = max(24, int((chart_w - bar_gap * (len(days) - 1)) / max(len(days), 1)))
    pieces: list[str] = [
        f'<line x1="{margin_left}" y1="{margin_top + chart_h}" x2="{width - margin_right}" y2="{margin_top + chart_h}" class="grid"/>'
    ]
    for i, day in enumerate(days):
        x = margin_left + i * (bar_w + bar_gap)
        bar_h = 0 if max_value == 0 else chart_h * int(day["total_tokens"]) / max_value
        y = margin_top + chart_h - bar_h
        color = "#2563eb" if int(day["events"]) else "#cbd5e1"
        pieces.append(f'<rect x="{x}" y="{y:.1f}" width="{bar_w}" height="{bar_h:.1f}" rx="7" fill="{color}"/>')
        pieces.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{max(22, y - 10):.1f}" class="value" text-anchor="middle">'
            f'{escape_xml(compact_number(day["total_tokens"]))}</text>'
        )
        pieces.append(f'<text x="{x + bar_w / 2:.1f}" y="{height - 42}" class="axis" text-anchor="middle">{escape_xml(day["label"])}</text>')
        pieces.append(
            f'<text x="{x + bar_w / 2:.1f}" y="{height - 22}" class="axis subtle" text-anchor="middle">'
            f'{int(day["events"])} prompts / avg {escape_xml(compact_number(day["avg_tokens"]))}</text>'
        )
    return SVG_TEMPLATE.format(width=width, height=height, title="Last 7 days: total and average per prompt", body="\n".join(pieces))


def svg_compact_bar(title: str, rows: list[dict[str, Any]], width: int = 560, height: int | None = None) -> str:
    rows = rows[:8]
    if not rows:
        return svg_empty(title, "No data available.", width, 220)
    margin_left = 150
    margin_right = 72
    margin_top = 52
    row_h = 34
    height = height or max(220, margin_top + row_h * len(rows) + 28)
    chart_w = width - margin_left - margin_right
    max_value = max([int(row["total_tokens"]) for row in rows] or [1]) or 1
    pieces: list[str] = []
    for i, row in enumerate(rows):
        y = margin_top + i * row_h
        bar_w = chart_w * int(row["total_tokens"]) / max_value
        color = PALETTE[i % len(PALETTE)]
        label = str(row["label"])
        pieces.append(f'<text x="18" y="{y + 21}" class="label">{escape_xml(label[:22])}</text>')
        pieces.append(f'<rect x="{margin_left}" y="{y}" width="{bar_w:.1f}" height="20" rx="5" fill="{color}"/>')
        pieces.append(
            f'<text x="{margin_left + bar_w + 8:.1f}" y="{y + 16}" class="value">'
            f'{escape_xml(compact_number(row["total_tokens"]))}</text>'
        )
        pieces.append(
            f'<text x="{width - 18}" y="{y + 16}" class="axis" text-anchor="end">'
            f'avg {escape_xml(compact_number(row["avg_tokens"]))}</text>'
        )
    return SVG_TEMPLATE.format(width=width, height=height, title=escape_xml(title), body="\n".join(pieces))


def svg_empty(title: str, message: str, width: int, height: int) -> str:
    body = (
        f'<rect x="24" y="72" width="{width - 48}" height="{height - 108}" rx="8" fill="#e2e8f0"/>'
        f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" class="label" text-anchor="middle">{escape_xml(message)}</text>'
    )
    return SVG_TEMPLATE.format(width=width, height=height, title=escape_xml(title), body=body)


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
    events = load_events(store)
    summary = summarize(events)
    rows = usage_rows(events)
    write_json(out_dir / "remotion-token-efficiency-data.json", remotion_payload(summary))
    write_json(out_dir / "dashboard-data.json", dashboard_payload(summary, rows))

    chart_assets = write_dashboard_charts(summary, rows, out_dir)
    index = html_report(summary, rows, chart_assets)
    html_path = out_dir / "index.html"
    html_path.write_text(index, encoding="utf-8")

    pdf_path: Path | None = None
    if args.pdf or args.pdf_path:
        pdf_path = Path(args.pdf_path) if args.pdf_path else out_dir / "token-efficiency-dashboard.pdf"
        pdf_result = render_pdf(html_path, pdf_path)
        if pdf_result:
            print(pdf_result)

    remotion_dir = Path(args.remotion_dir) if args.remotion_dir else out_dir / "remotion"
    if not args.skip_remotion:
        write_remotion_package(summary, remotion_dir)
    print(f"wrote {html_path}")
    for asset in chart_assets:
        print(f"wrote {out_dir / asset}")
    print(f"wrote {out_dir / 'dashboard-data.json'}")
    print(f"wrote {out_dir / 'remotion-token-efficiency-data.json'}")
    if pdf_path and pdf_path.exists():
        print(f"wrote {pdf_path}")
    if not args.skip_remotion:
        print(f"wrote {remotion_dir / 'package.json'}")
        print(f"preview with: cd {remotion_dir} && npm install && npm run studio")
        print(f"render still: cd {remotion_dir} && npm install && npm run still")
    return 0


def write_dashboard_charts(summary: dict[str, Any], rows: list[dict[str, Any]], out_dir: Path) -> list[str]:
    last7 = last_n_days(rows, 7)
    charts = {
        "token-efficiency-prompt-sequence.svg": svg_prompt_sequence(rows[-40:]),
        "token-efficiency-last-7-days.svg": svg_daily_rollup(last7),
        "token-efficiency-by-branch.svg": svg_compact_bar("Usage by branch", group_rows(rows, "branch")),
        "token-efficiency-by-model.svg": svg_compact_bar("Usage by model", group_rows(rows, "model")),
        "token-efficiency-by-kind.svg": svg_compact_bar("Usage by task kind", group_rows(rows, "kind")),
        "token-efficiency-by-tag.svg": svg_compact_bar("Usage by tag", group_rows(rows, "tags")),
    }
    assets: list[str] = []
    for name, svg in charts.items():
        (out_dir / name).write_text(svg, encoding="utf-8")
        assets.append(name)
    return assets


def dashboard_payload(summary: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = dashboard_metrics(summary, rows)
    recent_rows: list[dict[str, Any]] = []
    for row in rows[-25:]:
        recent_rows.append(
            {
                key: value
                for key, value in row.items()
                if key != "created"
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": summary.get("generated_at_utc"),
        "metrics": metrics,
        "last_7_days": last_n_days(rows, 7),
        "by_branch": group_rows(rows, "branch"),
        "by_model": group_rows(rows, "model"),
        "by_kind": group_rows(rows, "kind"),
        "by_tag": group_rows(rows, "tags"),
        "recent_prompts": recent_rows,
    }


def find_chrome() -> str | None:
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ]
    for candidate in candidates:
        if "/" in candidate and Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def render_pdf(html_path: Path, pdf_path: Path) -> str | None:
    chrome = find_chrome()
    if not chrome:
        return "warning: Chrome/Chromium not found; skipped PDF export"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            str(html_path.resolve()),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        return f"warning: PDF export failed: {proc.stderr.strip() or proc.stdout.strip()}"
    if not pdf_path.exists() or pdf_path.stat().st_size <= 0:
        return "warning: PDF export did not produce a non-empty file"
    return f"verified pdf file exists ({pdf_path.stat().st_size} bytes)"


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


def metric_card_html(label: str, value: str, context: str) -> str:
    return (
        '<article class="metric-card">'
        f'<div class="metric-label">{escape_xml(label)}</div>'
        f'<div class="metric-value">{escape_xml(value)}</div>'
        f'<div class="metric-context">{escape_xml(context)}</div>'
        "</article>"
    )


def recent_prompt_rows(rows: list[dict[str, Any]], limit: int = 12) -> str:
    recent = list(reversed(rows[-limit:]))
    if not recent:
        return '<tr><td colspan="8">No prompt events have been recorded yet.</td></tr>'
    table_rows: list[str] = []
    for row in recent:
        table_rows.append(
            "<tr>"
            f"<td>{escape_xml(row['created_label'])}</td>"
            f"<td>{escape_xml(row['branch'])}</td>"
            f"<td>{escape_xml(row['kind'])}</td>"
            f"<td>{escape_xml(row['model'])}</td>"
            f"<td>{escape_xml(row['tags'][:42])}</td>"
            f"<td class=\"num\">{compact_number(row['input_tokens'])}</td>"
            f"<td class=\"num\">{compact_number(row['output_tokens'])}</td>"
            f"<td class=\"num strong\">{compact_number(row['total_tokens'])}</td>"
            "</tr>"
        )
    return "\n".join(table_rows)


def concentration_note(rows: list[dict[str, Any]], field: str, label: str) -> str:
    groups = group_rows(rows, field)
    if not groups:
        return f"No {label} concentration yet."
    top = groups[0]
    total = sum(int(group["total_tokens"]) for group in groups) or 1
    share = int(top["total_tokens"]) / total
    return (
        f"{top['label']} leads {label} usage with {compact_number(top['total_tokens'])} tokens "
        f"across {top['events']} prompt(s), about {pct(share)} of tracked usage."
    )


def html_report(summary: dict[str, Any], rows: list[dict[str, Any]], assets: list[str]) -> str:
    metrics = dashboard_metrics(summary, rows)
    generated = display_time(parse_event_time({"created_at_utc": summary.get("generated_at_utc", "")}))
    branch_note = concentration_note(rows, "branch", "branch")
    model_note = concentration_note(rows, "model", "model")
    chart_map = {Path(asset).stem: asset for asset in assets}
    total_events = metrics["events"]
    total_tokens = metrics["total_tokens"]
    executive_summary = [
        f"<strong>{total_events} prompt events are logged.</strong> Current telemetry estimates {compact_number(total_tokens)} total tokens with an average of {compact_number(metrics['avg_tokens'])} tokens per prompt.",
        f"<strong>The last 7 days are shown even when days are empty.</strong> This prevents same-day prompts from collapsing into one unexplained dot while still preserving daily rollups.",
        f"<strong>Prompt-level timing is visible.</strong> The prompt sequence chart plots each recorded event separately, so multiple prompts on the same date remain distinct.",
    ]
    metric_cards = "\n".join(
        [
            metric_card_html("Tracked prompts", compact_number(metrics["events"]), "Immutable event JSON files"),
            metric_card_html("Total tokens", compact_number(metrics["total_tokens"]), f"Avg {compact_number(metrics['avg_tokens'])} per prompt"),
            metric_card_html("Last 7 days", compact_number(metrics["last7_tokens"]), f"{metrics['last7_events']} prompts, avg {compact_number(metrics['last7_avg'])}"),
            metric_card_html("API estimate", money(metrics["api_usd"]), f"{metrics['codex_credits']:.2f} Codex credits"),
            metric_card_html("Peak prompt", compact_number(metrics["top_prompt_tokens"]), metrics["top_prompt_label"]),
            metric_card_html("Worker usage", pct(metrics["worker_share"]), f"{metrics['worker_events']} prompt(s) used workers"),
            metric_card_html("Input/output mix", f"{compact_number(metrics['input_tokens'])} / {compact_number(metrics['output_tokens'])}", "Input tokens / output tokens"),
            metric_card_html("Cache rate", pct(metrics["cache_rate"]), f"{compact_number(metrics['cached_tokens'])} cached input tokens"),
        ]
    )
    summary_items = "\n".join(f"<li>{item}</li>" for item in executive_summary)
    prompt_chart = chart_map.get("token-efficiency-prompt-sequence", "token-efficiency-prompt-sequence.svg")
    daily_chart = chart_map.get("token-efficiency-last-7-days", "token-efficiency-last-7-days.svg")
    branch_chart = chart_map.get("token-efficiency-by-branch", "token-efficiency-by-branch.svg")
    model_chart = chart_map.get("token-efficiency-by-model", "token-efficiency-by-model.svg")
    kind_chart = chart_map.get("token-efficiency-by-kind", "token-efficiency-by-kind.svg")
    tag_chart = chart_map.get("token-efficiency-by-tag", "token-efficiency-by-tag.svg")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Token Efficiency Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #111827;
      --muted: #64748b;
      --line: #d7dee8;
      --panel: #ffffff;
      --soft: #f5f7fb;
      --blue: #2563eb;
      --green: #059669;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #e9edf4;
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 28px 24px 56px; }}
    header {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 18px;
    }}
    h1 {{ margin: 0 0 6px; font-size: 34px; line-height: 1.05; letter-spacing: 0; }}
    h2 {{ margin: 0 0 10px; font-size: 20px; letter-spacing: 0; }}
    h3 {{ margin: 0 0 8px; font-size: 15px; color: #334155; }}
    p {{ color: #334155; line-height: 1.5; margin: 0; }}
    .meta {{ color: var(--muted); font-size: 13px; text-align: right; min-width: 210px; }}
    .section {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin: 14px 0;
      box-shadow: 0 14px 34px rgba(15, 23, 42, 0.05);
    }}
    .summary-list {{ margin: 8px 0 0; padding-left: 21px; color: #243142; }}
    .summary-list li {{ margin: 7px 0; line-height: 1.45; }}
    .metric-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .metric-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 104px;
      background: linear-gradient(180deg, #ffffff 0%, #f8fafc 100%);
    }}
    .metric-label {{ color: var(--muted); font-size: 12px; font-weight: 750; text-transform: uppercase; letter-spacing: .05em; }}
    .metric-value {{ font-size: 28px; font-weight: 820; margin-top: 7px; color: #0f172a; }}
    .metric-context {{ margin-top: 4px; color: #475569; font-size: 13px; line-height: 1.3; }}
    .two-col {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 14px; }}
    .chart-frame {{ border: 1px solid var(--line); border-radius: 8px; background: var(--soft); padding: 10px; margin-top: 12px; }}
    .chart-frame img {{ display: block; width: 100%; height: auto; }}
    .note {{ margin-top: 10px; font-size: 14px; color: #334155; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #e2e8f0; padding: 9px 8px; text-align: left; vertical-align: top; }}
    th {{ color: #475569; font-size: 11px; text-transform: uppercase; letter-spacing: .05em; background: #f8fafc; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .strong {{ font-weight: 800; color: #0f172a; }}
    .caveats {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .caveat {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 13px; font-size: 14px; color: #334155; line-height: 1.4; }}
    @media (max-width: 900px) {{
      header, .two-col {{ grid-template-columns: 1fr; display: block; }}
      .metric-grid, .caveats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .meta {{ text-align: left; margin-top: 8px; }}
    }}
    @media (max-width: 620px) {{
      main {{ padding: 18px 12px 36px; }}
      .metric-grid, .caveats {{ grid-template-columns: 1fr; }}
      h1 {{ font-size: 28px; }}
      table {{ font-size: 12px; }}
    }}
    @media print {{
      body {{ background: #fff; }}
      main {{ max-width: none; padding: 18px; }}
      .section {{ box-shadow: none; break-inside: avoid; }}
    }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Token Efficiency Dashboard</h1>
      <p>Branch-safe Codex token telemetry, prompt-level detail, and daily rollups with average usage per prompt.</p>
    </div>
    <div class="meta">
      <div>Generated {escape_xml(generated)}</div>
      <div>{escape_xml(str(summary.get('generated_at_utc', '')))}</div>
    </div>
  </header>
  <section class="section">
    <h2>Executive Summary</h2>
    <ul class="summary-list">
      {summary_items}
    </ul>
    <div class="metric-grid">
      {metric_cards}
    </div>
  </section>
  <section class="section">
    <h2>Prompt-level timeline keeps same-day runs separate</h2>
    <p>Each point is a recorded prompt event ordered by timestamp. This fixes the previous one-dot-per-day behavior when several prompts happened on the same date.</p>
    <div class="chart-frame"><img src="{escape_xml(prompt_chart)}" alt="Prompt-level token usage sequence"></div>
  </section>
  <section class="section">
    <h2>Last 7 days show totals and average tokens per prompt</h2>
    <p>The daily view is still useful for operating cadence, but it now shows empty days, prompt counts, and average tokens per prompt so aggregation does not hide activity.</p>
    <div class="chart-frame"><img src="{escape_xml(daily_chart)}" alt="Last seven days token usage"></div>
  </section>
  <section class="two-col">
    <div class="section">
      <h2>Branch concentration</h2>
      <p>{escape_xml(branch_note)}</p>
      <div class="chart-frame"><img src="{escape_xml(branch_chart)}" alt="Token usage by branch"></div>
    </div>
    <div class="section">
      <h2>Model concentration</h2>
      <p>{escape_xml(model_note)}</p>
      <div class="chart-frame"><img src="{escape_xml(model_chart)}" alt="Token usage by model"></div>
    </div>
  </section>
  <section class="two-col">
    <div class="section">
      <h2>Task type mix</h2>
      <p>{escape_xml(concentration_note(rows, "kind", "task type"))}</p>
      <div class="chart-frame"><img src="{escape_xml(kind_chart)}" alt="Token usage by task kind"></div>
    </div>
    <div class="section">
      <h2>Tag mix</h2>
      <p>{escape_xml(concentration_note(rows, "tags", "tag"))}</p>
      <div class="chart-frame"><img src="{escape_xml(tag_chart)}" alt="Token usage by tag"></div>
    </div>
  </section>
  <section class="section">
    <h2>Recent prompt ledger</h2>
    <p>The table keeps exact prompt-level rows beside the rollups so outliers are auditable without reading raw JSON.</p>
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Branch</th>
          <th>Kind</th>
          <th>Model</th>
          <th>Tags</th>
          <th class="num">Input</th>
          <th class="num">Output</th>
          <th class="num">Total</th>
        </tr>
      </thead>
      <tbody>
        {recent_prompt_rows(rows)}
      </tbody>
    </table>
  </section>
  <section class="section">
    <h2>Caveats and operating notes</h2>
    <div class="caveats">
      <div class="caveat"><strong>Estimates:</strong> Use provider-reported token counts when available. Local estimates are directional when only text is supplied.</div>
      <div class="caveat"><strong>Source of truth:</strong> Immutable JSON events under <code>.codex/token-usage/events</code> drive this dashboard. Summaries can be regenerated after merges.</div>
      <div class="caveat"><strong>Grouping:</strong> Daily totals are rollups only. Prompt-level charts and the ledger preserve individual prompt events.</div>
    </div>
  </section>
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
    p_visualize.add_argument("--pdf", action="store_true", help="Also export the dashboard to PDF using Chrome/Chromium")
    p_visualize.add_argument("--pdf-path", help="Optional PDF output path; implies --pdf")
    p_visualize.set_defaults(func=visualize)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
