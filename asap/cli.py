"""ASAP command line.

  asap demo       run the failure scenarios end to end (default: all three)
  asap attack     run adversarial mock models against the guardrails
  asap storm      push a 5,000-alert storm through the ingestion funnel
  asap replay     replay a run from its audit log (no LLM calls)
  asap verify-audit  check a run's hash chain against its anchor
  asap doctor     show which LLM and policy engine will be used
  asap sim        run the simulator as a standalone HTTP service
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def _llm(choice: str, settings):  # type: ignore[no-untyped-def]
    from .agent.llm import select_llm

    return select_llm(choice, settings.llm)


def _approval_mode(arg: str | None) -> str:
    if arg:
        return arg
    return "prompt" if sys.stdin.isatty() else "auto"


def _pause(interactive: bool, prompt: str) -> bool:
    """Wait for Enter between scenarios. Returns False if the user asked to quit."""
    if not interactive:
        return True
    try:
        ans = console.input(f"\n[bold reverse] {prompt} [/] [dim](Enter to continue, q to quit)[/] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return ans.strip().lower() not in ("q", "quit", "exit")


def _scenario_intro(i: int, total: int, sc) -> None:  # type: ignore[no-untyped-def]
    body = f"[bold]What's broken:[/] {sc.fault}\n\n[bold]Watch for:[/]\n" + \
        "\n".join(f"  {n}. {w}" for n, w in enumerate(sc.watch_for, 1)) + \
        f"\n\n[bold]Expected outcome:[/] {sc.expected_outcome}"
    console.print(Panel(body, title=f"Scenario {i}/{total}: {sc.name}  -  {sc.title}", border_style="cyan",
                        padding=(1, 2)))


def _scenario_recap(sc, run, runs_dir: str) -> None:  # type: ignore[no-untyped-def]
    act = f"{run.proposal.action} {run.proposal.target} {run.proposal.params}" if run.proposal else "none proposed"
    verdict = f"{run.decision.verdict} (tier {run.decision.tier})" if run.decision else "-"
    matched = {"bad_deploy": run.outcome == "RESOLVED" and bool(run.proposal) and run.proposal.action == "rollback",
               "cpu_throttle": run.outcome == "RESOLVED" and bool(run.proposal) and run.proposal.action == "scale",
               "db_red_herring": run.outcome == "REPORT_ONLY" and run.proposal is None}.get(sc.name)
    check = "" if matched is None else ("[green]matches the expected outcome[/]" if matched
                                         else "[yellow]differs from the expected outcome[/]")
    if run.approval and not run.approval.get("approved"):
        check += f" [dim](approval not given: {run.approval.get('reason')}, so nothing was executed)[/]"
    console.print(Panel(
        f"[bold]Outcome:[/] {run.outcome}  {check}\n[bold]Action:[/] {act}\n[bold]Policy verdict:[/] {verdict}\n"
        f"[bold]Diagnosis:[/] {(run.diagnosis or {}).get('root_cause', '-')}\n\n"
        f"[dim]Report: {runs_dir}/{run.run_id}/report.md\nReplay: asap replay {run.run_id}[/]",
        title=f"Scenario {sc.name}: recap", border_style="green" if matched else "yellow"))


def cmd_demo(a: argparse.Namespace) -> int:
    from .console import RichUI
    from .control.approval import ApprovalGate
    from .harness import Env, run_scenario
    from .sim.scenarios import SCENARIOS

    names = list(SCENARIOS) if a.scenario == "all" else [a.scenario]
    llm = _llm(a.llm, a.settings)
    mode = _approval_mode(a.approve)
    interactive = sys.stdin.isatty() and not a.no_pause
    ui = RichUI(console, verbose=a.verbose)
    env = Env.create(Path(a.runs_dir), names[0], settings=a.settings)
    console.rule(f"[bold]ASAP demo[/]  reasoner={llm.name}:{llm.model}  policy={env.policy.name}  approvals={mode}")
    if llm.name == "deterministic-reasoner":
        console.print("[dim]No ANTHROPIC_API_KEY / OPENAI_BASE_URL set: using the deterministic reasoner (no LLM). "
                      "It uses the same tools, control plane and executor. Set ANTHROPIC_API_KEY to run with Claude.[/]")
    if interactive and len(names) > 1:
        console.print(f"[dim]{len(names)} scenarios, one at a time. Each starts from a fresh simulated cluster.[/]")
    results = []
    try:
        for i, n in enumerate(names, 1):
            sc = SCENARIOS[n]
            console.print()
            console.rule(f"[bold]Scenario {i}/{len(names)}: {n}[/]")
            _scenario_intro(i, len(names), sc)
            if not _pause(interactive, f"Start scenario {i}/{len(names)}"):
                break
            gate = ApprovalGate(mode, render=ui.approval_packet, ttl_seconds=a.settings.control.approval_ttl_s)
            run, _ = run_scenario(env, n, llm, gate, ui)
            results.append((n, sc, run))
            _scenario_recap(sc, run, a.runs_dir)
            if i < len(names) and not _pause(interactive, f"Next: scenario {i + 1}/{len(names)}, {names[i]}"):
                break
    finally:
        env.close()
    if len(results) > 1:
        t = Table(title="Summary")
        for col in ("scenario", "outcome", "action", "verdict", "expected"):
            t.add_column(col)
        for n, sc, run in results:
            act = f"{run.proposal.action} {run.proposal.target}" if run.proposal else "-"
            t.add_row(n, run.outcome or "", act, run.decision.verdict if run.decision else "-", sc.expected_outcome)
        console.print(t)
    console.print(f"Artifacts in [bold]{a.runs_dir}/[/]: report.md, audit.jsonl, spans.jsonl per run; metrics.prom")
    return 0


def cmd_attack(a: argparse.Namespace) -> int:
    from .agent.adversarial import run_attacks
    from .console import RichUI
    from .harness import Env

    env = Env.create(Path(a.runs_dir))  # built-in defaults, not env overrides: the suite must be reproducible
    console.rule(f"[bold]Adversarial mock models vs. guardrails[/]  policy={env.policy.name}")
    try:
        rows = run_attacks(env, RichUI(console) if a.verbose else None, a.only)
    finally:
        env.close()
    t = Table(title="Every attack must end without an unsafe action", show_lines=True)
    for col in ("attack", "what the model tried", "outcome", "blocked by", "ok"):
        t.add_column(col, overflow="fold")
    for r in rows:
        outcome = " → ".join(o or "" for o in r["outcomes"])
        extra = f"\n({r['redacted']} injected strings redacted)" if r["redacted"] else ""
        t.add_row(r["attack"], r["description"] + extra, outcome, r["blocked_by"][:160],
                  "[green]PASS[/]" if r["passed"] else "[red]FAIL[/]")
    console.print(t)
    ok = all(r["passed"] for r in rows)
    console.print("[bold green]All attacks contained.[/]" if ok else "[bold red]Some attacks were not contained.[/]")
    return 0 if ok else 1


def cmd_storm(a: argparse.Namespace) -> int:
    from .ingest.storm import CAPACITY_HEADROOM_MULTIPLIER, capacity_model, run_storm
    from .sim.scenarios import NOW

    stats, incidents = run_storm(NOW, a.alerts)
    t = Table(title=f"Alert storm: {a.alerts:,} alerts in one minute")
    t.add_column("stage")
    t.add_column("removed", justify="right")
    t.add_column("remaining", justify="right")
    left = stats.received
    t.add_row("received", "", f"{left:,}")
    for label, n in (("resolved notifications", stats.resolved_or_expired), ("flapping (>=2 flips/10 min)", stats.flapping),
                     ("debounced (for: 2m)", stats.debounced), ("duplicate fingerprints (5 min)", stats.duplicates)):
        left -= n
        t.add_row(label, f"{n:,}", f"{left:,}")
    t.add_row("grouped by service+alertname", "", f"{stats.groups:,} groups")
    t.add_row("correlated via dependency graph", "", f"[bold]{stats.incidents} incidents[/]")
    console.print(t)
    for i in incidents:
        console.print(f"  {i.incident_id}  priority={i.priority}  cell={i.cell}  root=[bold]{i.root_service}[/]  "
                      f"services={i.services}  alerts={len(i.alerts)}"
                      + ("  [yellow]suspected shared-infrastructure cause[/]" if i.suspected_shared_cause else ""))
    for note in stats.notes:
        console.print(f"  [yellow]{note}[/]")
    k = CAPACITY_HEADROOM_MULTIPLIER
    cap = capacity_model(max(len(incidents), 1) * k)
    console.print(f"\nOnly {len(incidents)} LLM runs needed instead of {a.alerts:,}. Capacity model at {k}x this rate: {cap}")
    return 0


def _replay_detail(event: str, p: dict) -> str:
    if event == "transition":
        return f"{p['from']} → {p['to']}  {p.get('reason', '')}"
    if event == "llm_turn":
        thought = f"\n  thought: {p['thought'][:160]}" if p.get("thought") else ""
        return f"[{p['phase']}] {p['tool']} {json.dumps(p['args'])[:140]}{thought}"
    if event == "tool_result":
        return f"{p['evidence_id']} {p['tool']}"
    if event == "decision":
        return f"{p['verdict']} (tier {p['tier']}): {'; '.join(p['reasons'])}  blast={p['blast_radius']}"
    if event == "approval":
        return f"approved={p['approved']} by {p['approvers']} ({p['reason']})"
    if event == "diagnosis":
        return f"{p['category']} conf={p['confidence']}: {p['root_cause'][:200]}"
    return json.dumps(p, default=str)[:180]


def cmd_tokens(a: argparse.Namespace) -> int:
    from .agent.context import CHARS_PER_TOKEN
    from .agent.tokenreport import CACHE_READ, CACHE_WRITE, measure

    rows = measure(Path(a.runs_dir) / "tokens")
    t = Table(title=f"Input tokens per run (estimated: chars/{CHARS_PER_TOKEN:g}). Cost-equivalent models prompt "
                    f"caching (read {CACHE_READ:g}x, write {CACHE_WRITE:g}x)")
    for col in ("run", "LLM calls", "peak context", "total input", "cost-equiv. (cached)", "compactions", "outcome"):
        t.add_column(col, justify="left" if col in ("run", "outcome") else "right")
    for r in rows:
        t.add_row(r["run"], str(r["llm_calls"]), f"{r['peak_context']:,}", f"{r['total_input']:,}",
                  f"{r['cost_equivalent_cached']:,}", str(r["compactions"]),
                  r["outcome"] + (" (token budget)" if "budget" in r["reason"] else ""))
    console.print(t)
    return 0


def cmd_replay(a: argparse.Namespace) -> int:
    path = Path(a.runs_dir) / a.run_id / "audit.jsonl"
    if not path.exists():
        console.print(f"[red]no audit log at {path}[/]")
        return 1
    t = Table(title=f"Replay {a.run_id} (from audit log; no LLM calls)", show_lines=False)
    for col in ("seq", "actor", "event", "detail"):
        t.add_column(col, overflow="fold")
    for line in path.read_text().splitlines():
        r = json.loads(line)
        detail = _replay_detail(r["event"], r["payload"])
        t.add_row(str(r["seq"]), r["actor"], r["event"], detail)
    console.print(t)
    return cmd_verify(a)


def cmd_verify(a: argparse.Namespace) -> int:
    from .audit.log import verify_chain

    ok, msg = verify_chain(Path(a.runs_dir) / a.run_id / "audit.jsonl")
    console.print(f"audit chain: [{'green' if ok else 'red'}]{msg}[/]")
    return 0 if ok else 1


def cmd_doctor(a: argparse.Namespace) -> int:
    from .control.policy import PolicyEngine

    llm = _llm(a.llm, a.settings)
    pe = PolicyEngine(settings=a.settings.control)
    console.print(f"python      {sys.version.split()[0]}")
    console.print(f"reasoner    {llm.name}:{llm.model}")
    console.print(f"policy      {pe.name}  bundle {pe.digest}")
    console.print(f"thresholds  {pe.constants}")
    overrides = {k: v for k, v in a.settings.env_vars().items() if k in os.environ}
    console.print(f"overrides   {overrides or 'none (built-in defaults; see asap/config.py)'}")
    if pe.name.startswith("none"):
        console.print("[yellow]No Rego evaluator: every action will be denied (fail closed). Run `make setup`.[/]")
    return 0


def cmd_sim(a: argparse.Namespace) -> int:
    from .sim.server import serve_forever

    serve_forever(a.scenario, a.host, a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    from .config import Settings
    from .sim.scenarios import SCENARIOS

    try:
        settings = Settings.from_env()  # the only place the environment is read for settings
    except ValueError as e:
        console.print(f"[red]invalid setting: {e}[/]")
        return 2
    ap = argparse.ArgumentParser(prog="asap", description="Autonomous SRE Agentic Platform (PoC)")
    ap.add_argument("--runs-dir", default=settings.app.runs_dir)
    ap.add_argument("--log-level", default=settings.app.log_level,
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="operational logs to stderr (default WARNING)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    llm_help = "auto: Claude if ANTHROPIC_API_KEY, OpenAI-compatible if OPENAI_BASE_URL/OPENAI_API_KEY, else scripted"

    d = sub.add_parser("demo", help="run failure scenarios end to end")
    d.add_argument("--scenario", default="all", choices=["all", *SCENARIOS])
    d.add_argument("--llm", default="auto", choices=["auto", "scripted", "anthropic", "openai", "ollama"], help=llm_help)
    d.add_argument("--approve", choices=["prompt", "auto", "deny", "timeout"],
                   help="approval mode (default: prompt in a terminal, auto otherwise)")
    d.add_argument("--no-pause", action="store_true", help="run all scenarios back to back without waiting for Enter")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(fn=cmd_demo)

    at = sub.add_parser("attack", help="adversarial mock models vs. guardrails")
    at.add_argument("--only", nargs="*")
    at.add_argument("-v", "--verbose", action="store_true")
    at.set_defaults(fn=cmd_attack)

    st = sub.add_parser("storm", help="alert storm through the ingestion funnel")
    st.add_argument("--alerts", type=int, default=5000)
    st.set_defaults(fn=cmd_storm)

    tk = sub.add_parser("tokens", help="measure per-run LLM input tokens, compaction and budget")
    tk.set_defaults(fn=cmd_tokens)

    rp = sub.add_parser("replay", help="replay a run from its audit log")
    rp.add_argument("run_id")
    rp.set_defaults(fn=cmd_replay)

    va = sub.add_parser("verify-audit", help="verify a run's audit hash chain")
    va.add_argument("run_id")
    va.set_defaults(fn=cmd_verify)

    dr = sub.add_parser("doctor", help="show configuration")
    dr.add_argument("--llm", default="auto")
    dr.set_defaults(fn=cmd_doctor)

    sm = sub.add_parser("sim", help="run the simulator standalone")
    sm.add_argument("--scenario", default="bad_deploy", choices=list(SCENARIOS))
    sm.add_argument("--host", default="127.0.0.1")
    sm.add_argument("--port", type=int, default=8080)
    sm.set_defaults(fn=cmd_sim)

    a = ap.parse_args(argv)
    a.settings = settings
    _configure_logging(a.log_level)
    return int(a.fn(a))


def _configure_logging(level: str) -> None:
    """Operational logs (retries, fallbacks, swallowed errors) go to stderr via the logging module.
    The audit trail is separate: runs/<run_id>/audit.jsonl."""
    from rich.logging import RichHandler

    logging.basicConfig(level=level, format="%(name)s: %(message)s", datefmt="[%X]",
                        handlers=[RichHandler(console=Console(stderr=True), show_path=False, markup=False)])
    for noisy in ("httpx", "httpcore", "uvicorn", "uvicorn.error", "anthropic"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLevelName(level)))


if __name__ == "__main__":
    raise SystemExit(main())
