"""Console rendering. The orchestrator talks to a UI object; tests use NullUI."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STATE_STYLE = {"RESOLVED": "bold green", "REPORT_ONLY": "bold yellow", "REVERTED_ESCALATED": "bold red",
               "AWAIT_APPROVAL": "bold magenta", "POLICY_CHECK": "bold cyan", "EXECUTE": "bold blue"}


class NullUI:
    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return lambda *a, **k: None


def _short(obj: object, n: int = 160) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _result_line(tool: str, r: dict) -> str:
    if tool == "query_metrics":
        cp = f", changed {r.get('minutes_since_change')} min ago" if r.get("change_point") else ", no change point"
        return f"{r['metric']}({r['service']}): baseline {r['baseline']} -> current {r['current']}{cp}"
    if tool == "search_logs":
        cl = r.get("clusters", [])
        top = "; ".join(f"{c['count']}x {c['level']} {c['template'][:70]}" for c in cl[:2])
        return f"{len(cl)} clusters: {top}" + (f"  [{r['_gateway_note']}]" if r.get("_gateway_note") else "")
    if tool == "get_traces":
        d = (r.get("downstream") or [{}])[0]
        return (f"root p99 {r.get('root_p99_ms')} ms; top downstream {d.get('peer.service')} "
                f"({d.get('share_of_root_p99', 0):.0%} of p99, db.system={d.get('db.system')})") if d else \
            f"root p99 {r.get('root_p99_ms')} ms; no downstream calls"
    if tool == "get_deployment_history":
        return "; ".join(f"rev {x['revision']} {x['version']} ({x['age_minutes']:.0f}m ago{', current' if x['current'] else ''})"
                         for x in r.get("revisions", [])[:3])
    if tool == "get_resource_state":
        return f"{r['kind']} tier-{r['tier']} replicas={r['replicas']} hpa={r['hpa']} pdb={r['pdb']}"
    return _short(r)


class RichUI:
    def __init__(self, console: Console | None = None, verbose: bool = False) -> None:
        self.c = console or Console()
        self.verbose = verbose

    def start(self, run, incident) -> None:  # type: ignore[no-untyped-def]
        self.c.print(Panel.fit(
            f"[bold]{incident.incident_id}[/]  severity={incident.severity}  root candidate=[bold]{incident.root_service}[/]\n"
            f"affected: {', '.join(incident.services)}\n{incident.summary}\n"
            f"run {run.run_id}  reasoner: [bold]{run.llm_name}[/]", title="Incident", border_style="red"))

    def transition(self, run, src: str, dst: str, reason: str) -> None:  # type: ignore[no-untyped-def]
        self.c.print(Text.assemble(("  ▸ ", "dim"), (f"{src} → ", "dim"), (dst, STATE_STYLE.get(dst, "bold")),
                                   (f"  {reason}" if reason else "", "dim")))

    def llm(self, run, phase: str, resp, reasoning: str = "") -> None:  # type: ignore[no-untyped-def]
        if reasoning:
            self.c.print(f"    [italic cyan]reasoning:[/] {_short(reasoning, 220)}")
        if resp.tool_name in ("submit_plan",):
            for h in resp.tool_args.get("hypotheses", []):
                self.c.print(f"    [cyan]hypothesis:[/] {h}")
        elif resp.tool_name == "submit_diagnosis":
            a = resp.tool_args
            self.c.print(Panel(f"{a.get('root_cause')}\n[dim]category={a.get('category')} confidence={a.get('confidence')} "
                               f"action={a.get('recommended_action')} evidence={a.get('evidence_ids')}[/]",
                               title="Diagnosis", border_style="cyan"))
        elif resp.tool_name.startswith("propose_") or resp.tool_name == "no_action":
            args = {k: v for k, v in resp.tool_args.items() if k != "reasoning"}
            self.c.print(f"    [bold cyan]proposal:[/] {resp.tool_name} {_short(args, 200)}")
        elif self.verbose:
            self.c.print(f"    [cyan]call[/] {resp.tool_name} {_short(resp.tool_args)}")

    def tool(self, run, name: str, args: dict, result: dict) -> None:  # type: ignore[no-untyped-def]
        self.c.print(f"    [green]{result.get('evidence_id')}[/] {name}: {_short(_result_line(name, result), 190)}")

    def rejected(self, run, name: str, err: str) -> None:  # type: ignore[no-untyped-def]
        self.c.print(f"    [bold red]✗ rejected[/] {name}: {_short(err, 190)}")

    def decision(self, run, d) -> None:  # type: ignore[no-untyped-def]
        t = Table(show_header=False, box=None, padding=(0, 1))
        color = {"allow": "green", "require_approval": "magenta", "deny": "red"}[d.verdict]
        t.add_row("verdict", f"[bold {color}]{d.verdict.upper()}[/] (tier {d.tier})")
        t.add_row("reasons", "\n".join(d.reasons))
        t.add_row("blast radius", str(d.blast_radius))
        if d.dry_run:
            t.add_row("dry-run", "ok " + _short(d.dry_run.diff) if d.dry_run.ok else f"FAILED {d.dry_run.errors}")
        t.add_row("engine", d.policy_engine)
        self.c.print(Panel(t, title="Control plane decision (OPA/Rego)", border_style=color))

    def approval(self, run, res: dict) -> None:  # type: ignore[no-untyped-def]
        ok = res["approved"]
        self.c.print(f"    [{'green' if ok else 'red'}]approval: {'APPROVED' if ok else 'NOT APPROVED'}[/] "
                     f"by {res['approvers'] or '-'} via {res['channel']} ({res['reason']})")

    def approval_packet(self, packet: dict) -> None:
        body = (f"[bold]{packet['proposal']['action']} {packet['proposal']['target']}[/] {packet['proposal']['params']}\n"
                f"why: {packet['diagnosis']['root_cause']}\n"
                f"diff: {packet['dry_run_diff']}\n"
                f"policy: {'; '.join(packet['policy_reasons'])}   blast radius: {packet['blast_radius']}\n"
                f"evidence: {', '.join(e['id'] + ' ' + e['tool'] for e in packet['evidence'])}\n"
                f"approvals required: {packet['required_approvals']}   bound to state {packet['state_hash']}")
        self.c.print(Panel(body, title="Approval request (Slack stand-in)", border_style="magenta"))

    def execution(self, run, ex: dict) -> None:  # type: ignore[no-untyped-def]
        self.c.print(f"    [blue]executed:[/] {_short(ex.get('result', ex), 200)}")

    def verification(self, run, v: dict) -> None:  # type: ignore[no-untyped-def]
        ok = v["recovered"]
        self.c.print(f"    [{'green' if ok else 'red'}]verify after {v['waited_minutes']} min:[/] firing before "
                     f"{v['firing_before']} → after {v['firing_after'] or 'none'}")

    def final(self, run, report: Path) -> None:  # type: ignore[no-untyped-def]
        style = STATE_STYLE.get(run.outcome or "", "bold")
        self.c.print(Panel.fit(
            f"[{style}]{run.outcome}[/]: {run.outcome_reason}\n"
            f"steps={run.steps} tool_calls={run.tool_calls} input tokens={run.budget_tokens_in:,} "
            f"(peak context {run.peak_context_tokens:,}, compactions {run.compactions}) output={run.tokens_out:,} "
            f"untrusted strings redacted={len(run.flagged_untrusted)}\n"
            f"report: {report}\naudit:  {report.parent / 'audit.jsonl'}   replay: asap replay {run.run_id}",
            title="Outcome", border_style=style.split()[-1]))
