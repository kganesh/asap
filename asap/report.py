"""Human-readable incident report (the artifact handed to on-call when ASAP doesn't act, and the
post-mortem record when it does)."""

from __future__ import annotations

from pathlib import Path


def write_report(run_dir: Path, run, incident) -> Path:  # type: ignore[no-untyped-def]
    L: list[str] = []
    a = L.append
    a(f"# Incident report: {incident.incident_id}\n")
    a(f"**Outcome:** `{run.outcome}` - {run.outcome_reason}  ")
    a(f"**Run:** `{run.run_id}` - reasoner `{run.llm_name}` - steps {run.steps}, tool calls {run.tool_calls}, "
      f"tokens {run.tokens_in} in / {run.tokens_out} out\n")
    a("## Alerts\n")
    a("| Alert | Service | Severity | Started | Detail |\n|---|---|---|---|---|")
    for al in incident.alerts:
        lb = al["labels"]
        a(f"| {lb['alertname']} | {lb['service']} | {lb.get('severity')} | {al['startsAt']} | "
          f"{al.get('annotations', {}).get('description', '')} |")
    if run.plan:
        a("\n## Investigation plan\n")
        for h in run.plan.get("hypotheses", []):
            a(f"- {h}")
        if run.replans:
            a(f"\n_Replanned {run.replans} time(s) as evidence arrived._")
    if run.diagnosis:
        d = run.diagnosis
        a("\n## Diagnosis\n")
        a(f"{d['root_cause']}\n")
        a(f"- Root service: `{d['root_service']}` - category `{d['category']}` - confidence {d['confidence']}")
        a(f"- Recommended action: `{d['recommended_action']}`")
    if run.evidence:
        a("\n## Evidence\n")
        a("| ID | Tool | Arguments | Cited |\n|---|---|---|---|")
        cited = set((run.diagnosis or {}).get("evidence_ids", []))
        for e in run.evidence.values():
            args = ", ".join(f"{k}={v}" for k, v in e.args.items())
            a(f"| `{e.evidence_id}` | {e.tool} | {args} | {'yes' if e.evidence_id in cited else ''} |")
    if run.proposal:
        p = run.proposal
        a("\n## Proposed remediation\n")
        a(f"`{p.action}` on `{p.target}` with {p.params} - {p.rationale}")
    if run.decision:
        d = run.decision
        a("\n## Control plane decision\n")
        a(f"- Verdict: **{d.verdict}** (tier {d.tier}), blast radius {d.blast_radius}, engine `{d.policy_engine}`")
        for r in d.reasons:
            a(f"- {r}")
        if d.dry_run:
            a(f"- Dry-run: {'ok' if d.dry_run.ok else 'failed'} {d.dry_run.diff or d.dry_run.errors}")
    if run.approval:
        ap = run.approval
        a("\n## Approval\n")
        a(f"{'Approved' if ap['approved'] else 'Not approved'} by {ap['approvers']} via {ap['channel']} - {ap['reason']}")
    if run.verification:
        v = run.verification
        a("\n## Verification\n")
        a(f"Waited {v['waited_minutes']} min. Firing before: {v['firing_before']}; after: {v['firing_after'] or 'none'}.")
        if v.get("revert"):
            a(f"\nRevert rule: {v['revert']['rule']}")
    a("\n## State transitions\n")
    for src, dst, why in run.transitions:
        a(f"1. `{src}` -> `{dst}` {why}")
    if run.flagged_untrusted:
        a(f"\n> {len(run.flagged_untrusted)} instruction-like string(s) in telemetry were redacted by the gateway.")
    a("\n---\nFull reasoning trail: `audit.jsonl` (hash-chained). Spans: `spans.jsonl`.")
    path = run_dir / "report.md"
    path.write_text("\n".join(L) + "\n")
    return path
