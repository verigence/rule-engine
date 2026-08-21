#!/usr/bin/env python3
"""audit-test-client/client.py

End-to-end test client for the Verigence Price Anomaly Rule Engine.

Pipeline:
  1. Create a DI subject
  2. Upload document files to DI
  3. Poll until DI processing is complete
  4. Confirm each document
  5. Call the audit rule engine
  6. Print colour-coded findings

Usage:
    python client.py --help
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _check(resp: httpx.Response, label: str) -> dict[str, Any]:
    """Raise a clear error on non-2xx; return parsed JSON body."""
    if resp.status_code >= 400:
        console.print(f"[red]✗ {label} failed [{resp.status_code}]:[/red]")
        try:
            console.print_json(resp.text)
        except Exception:  # noqa: BLE001
            console.print(resp.text)
        sys.exit(1)
    return resp.json()


def _ok_data(resp: httpx.Response, label: str) -> Any:
    """Check response and unwrap the .data envelope."""
    body = _check(resp, label)
    # Audit service wraps in {errorCode, errorMessage, data}
    return body.get("data", body)


# ── DI helpers ────────────────────────────────────────────────────────────────

def di_create_subject(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    token: str,
    subject_id: str | None = None,
) -> str:
    """POST /v1/tenants/{tid}/subjects — returns subject_id."""
    sid = subject_id or str(uuid.uuid4())
    resp = client.post(
        f"{di_url}/v1/tenants/{tenant_id}/subjects",
        json={"subjectId": sid, "metadata": {}},
        headers=_headers(token),
    )
    # 409 = already exists (idempotent)
    if resp.status_code == 409:
        console.print(f"[yellow]⚠ Subject {sid} already exists — reusing.[/yellow]")
        return sid
    _check(resp, "create_subject")
    console.print(f"[green]✓ Subject created:[/green]   {sid}")
    return sid


def di_upload_document(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    subject_id: str,
    doc_type: str,
    file_path: Path,
    token: str,
) -> str:
    """POST /v1/tenants/{tid}/subjects/{sid}/documents — returns document_id."""
    with file_path.open("rb") as fh:
        resp = client.post(
            f"{di_url}/v1/tenants/{tenant_id}/subjects/{subject_id}/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (file_path.name, fh, "application/octet-stream")},
            data={"documentType": doc_type},
            timeout=60.0,
        )
    body = _check(resp, f"upload:{doc_type}")
    doc_id = (body.get("data") or body).get("documentId", "<unknown>")
    console.print(f"[green]✓ Uploaded:[/green]          {doc_type:<30} → {doc_id}")
    return doc_id


def di_poll_processing(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    subject_id: str,
    doc_ids: list[str],
    token: str,
    max_attempts: int = 20,
    interval: float = 6.0,
) -> None:
    """Poll GET /v1/tenants/{tid}/documents/{docId} until all reach CONFIRMED or FAILED."""
    pending = set(doc_ids)
    for attempt in range(1, max_attempts + 1):
        if not pending:
            break
        still_pending: set[str] = set()
        for doc_id in list(pending):
            resp = client.get(
                f"{di_url}/v1/tenants/{tenant_id}/documents/{doc_id}",
                headers=_headers(token),
            )
            body = _check(resp, f"poll:{doc_id}")
            data = body.get("data", body)
            status = data.get("processingStatus") or data.get("status", "")
            if status in ("COMPLETED", "CONFIRMED", "READY_FOR_CONFIRMATION", "FAILED"):
                if status == "FAILED":
                    console.print(f"[red]✗ Document {doc_id} processing FAILED[/red]")
            else:
                still_pending.add(doc_id)
        pending = still_pending
        if pending:
            console.print(
                f"[yellow]⏳ Polling DI status:[/yellow] {attempt}/{max_attempts} "
                f"— {len(pending)} document(s) still processing…"
            )
            time.sleep(interval)
    if pending:
        console.print(
            f"[red]✗ Timed out waiting for documents: {pending}[/red]"
        )
        sys.exit(1)
    console.print("[green]✓ All documents processed.[/green]")


def di_confirm_document(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    doc_id: str,
    token: str,
) -> None:
    """POST /v1/tenants/{tid}/documents/{docId}/confirm"""
    resp = client.post(
        f"{di_url}/v1/tenants/{tenant_id}/documents/{doc_id}/confirm",
        json={},
        headers=_headers(token),
    )
    if resp.status_code == 409:
        console.print(f"[yellow]⚠ Document {doc_id} already confirmed.[/yellow]")
        return
    _check(resp, f"confirm:{doc_id}")
    console.print(f"[green]✓ Confirmed:[/green]         {doc_id}")


# ── Audit helpers ─────────────────────────────────────────────────────────────

def run_audit(
    client: httpx.Client,
    audit_url: str,
    tenant_id: str,
    subject_id: str,
    token: str,
    mode: str = "full",
    phase: str | None = None,
) -> dict[str, Any]:
    """Call the audit API and return the data payload."""
    base = f"{audit_url}/v1/tenants/{tenant_id}"
    if mode == "full":
        url = f"{base}/subjects/{subject_id}/audit"
    elif mode == "phase" and phase:
        url = f"{base}/subjects/{subject_id}/audit/{phase.lower()}"
    elif mode == "cross-case":
        url = f"{base}/audit/cross-case-scan"
    else:
        raise ValueError(f"Unknown mode {mode!r}")

    resp = client.post(url, json={}, headers=_headers(token), timeout=120.0)
    data = _ok_data(resp, f"audit:{mode}")
    return data  # type: ignore[return-value]


def _print_findings(anomalies: list[dict[str, Any]]) -> None:
    """Print a colour-coded findings table."""
    if not anomalies:
        console.print("[green]✓ No anomalies found.[/green]")
        return

    table = Table(
        title="Audit Findings",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold white on dark_blue",
    )
    table.add_column("Rule",     style="cyan",  no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Category", style="dim")
    table.add_column("Detail")
    table.add_column("Left",     style="dim", no_wrap=True)
    table.add_column("Right",    style="dim", no_wrap=True)

    sev_colours = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "blue"}
    for a in anomalies:
        sev = a.get("severity", "?")
        colour = sev_colours.get(sev, "white")
        table.add_row(
            a.get("ruleCode", ""),
            f"[{colour}]{sev}[/{colour}]",
            a.get("category", ""),
            a.get("detail", ""),
            str(a.get("leftValue", "")),
            str(a.get("rightValue", "")),
        )
    console.print(table)


def _print_verdict(data: dict[str, Any]) -> None:
    """Print a summary line."""
    verdict = data.get("verdict", "?")
    s = data.get("summary", {})
    colour = "green" if verdict == "PASS" else "red"
    console.print(
        f"[{colour}]✓ Audit complete:[/{colour}]   "
        f"verdict=[bold]{verdict}[/bold]  "
        f"rules={s.get('rulesEvaluated', '?')}  "
        f"pass={s.get('pass', '?')}  "
        f"fail={s.get('fail', '?')}  "
        f"skipped={s.get('skipped', '?')}  "
        f"[red]critical={s.get('critical', 0)}[/red]  "
        f"[yellow]warning={s.get('warning', 0)}[/yellow]"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.group()
@click.option(
    "--di-url",
    envvar="AUDIT_DI_URL",
    required=True,
    help="DI base URL, e.g. https://verigence-di.up.railway.app",
)
@click.option(
    "--audit-url",
    envvar="AUDIT_URL",
    required=True,
    help="Audit service base URL, e.g. https://audit-api.up.railway.app",
)
@click.option(
    "--tenant-id",
    envvar="AUDIT_TENANT_ID",
    required=True,
    help="Tenant UUID",
)
@click.option(
    "--token",
    envvar="AUDIT_TOKEN",
    required=True,
    help='Bearer JWT. Mock format: mock.<tenantId>.<actorId>.TENANT_ADMIN',
)
@click.option(
    "--subject-id",
    envvar="AUDIT_SUBJECT_ID",
    default=None,
    help="Subject UUID (auto-generated if omitted)",
)
@click.option(
    "--doc",
    "docs",
    multiple=True,
    metavar="TYPE:PATH",
    help="Document to upload. Repeat for multiple. e.g. booking_docket:./booking.pdf",
)
@click.option(
    "--skip-upload",
    is_flag=True,
    default=False,
    help="Skip DI upload/confirm — audit an already-confirmed subject directly.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    di_url: str,
    audit_url: str,
    tenant_id: str,
    token: str,
    subject_id: str | None,
    docs: tuple[str, ...],
    skip_upload: bool,
) -> None:
    """Verigence Audit Test Client — end-to-end pipeline tester."""
    ctx.ensure_object(dict)
    ctx.obj["di_url"]     = di_url.rstrip("/")
    ctx.obj["audit_url"]  = audit_url.rstrip("/")
    ctx.obj["tenant_id"]  = tenant_id
    ctx.obj["token"]      = token
    ctx.obj["subject_id"] = subject_id
    ctx.obj["docs"]       = docs
    ctx.obj["skip_upload"] = skip_upload


def _upload_and_confirm(ctx_obj: dict[str, Any]) -> str:
    """
    Shared logic: create subject → upload docs → poll → confirm.
    Returns subject_id.
    """
    di_url    = ctx_obj["di_url"]
    tenant_id = ctx_obj["tenant_id"]
    token     = ctx_obj["token"]
    docs      = ctx_obj["docs"]
    skip      = ctx_obj["skip_upload"]
    subject_id = ctx_obj["subject_id"]

    if skip:
        if not subject_id:
            console.print("[red]✗ --subject-id is required when --skip-upload is set.[/red]")
            sys.exit(1)
        console.print(f"[dim]Skipping DI upload — using subject {subject_id}[/dim]")
        return subject_id

    with httpx.Client(timeout=30.0) as client:
        subject_id = di_create_subject(
            client, di_url, tenant_id, token, subject_id
        )

        if not docs:
            console.print(
                "[yellow]⚠ No --doc files provided. Auditing with no documents "
                "(all rules will be SKIPPED).[/yellow]"
            )
            return subject_id

        doc_ids: list[str] = []
        for raw in docs:
            if ":" not in raw:
                console.print(f"[red]✗ --doc must be TYPE:PATH, got: {raw!r}[/red]")
                sys.exit(1)
            doc_type, _, path_str = raw.partition(":")
            file_path = Path(path_str)
            if not file_path.exists():
                console.print(f"[red]✗ File not found: {file_path}[/red]")
                sys.exit(1)
            doc_id = di_upload_document(
                client, di_url, tenant_id, subject_id, doc_type.strip(), file_path, token
            )
            doc_ids.append(doc_id)

        di_poll_processing(client, di_url, tenant_id, subject_id, doc_ids, token)

        for doc_id in doc_ids:
            di_confirm_document(client, di_url, tenant_id, doc_id, token)

    return subject_id


# ── audit group ───────────────────────────────────────────────────────────────

@cli.group()
def audit() -> None:
    """Run audit rules."""


@audit.command("full")
@click.pass_context
def audit_full(ctx: click.Context) -> None:
    """Run all 85 rules (full within-case audit)."""
    o = ctx.obj
    subject_id = _upload_and_confirm(o)
    with httpx.Client(timeout=120.0) as client:
        data = run_audit(client, o["audit_url"], o["tenant_id"], subject_id, o["token"], mode="full")
    _print_verdict(data)
    _print_findings(data.get("anomalies", []))


@audit.command("phase")
@click.option(
    "--phase",
    required=True,
    type=click.Choice(["booking", "delivery", "finance", "exchange", "corporate"], case_sensitive=False),
    help="Phase to audit.",
)
@click.pass_context
def audit_phase(ctx: click.Context, phase: str) -> None:
    """Run rules for a single process phase."""
    o = ctx.obj
    subject_id = _upload_and_confirm(o)
    with httpx.Client(timeout=120.0) as client:
        data = run_audit(
            client, o["audit_url"], o["tenant_id"], subject_id, o["token"],
            mode="phase", phase=phase,
        )
    _print_verdict(data)
    _print_findings(data.get("anomalies", []))


@audit.command("cross-case")
@click.pass_context
def audit_cross_case(ctx: click.Context) -> None:
    """Run cross-case duplicate scan across all subjects for this tenant."""
    o = ctx.obj
    # Cross-case doesn't need a subject — but still run upload if requested
    if not o["skip_upload"] and o["docs"]:
        _upload_and_confirm(o)
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{o['audit_url']}/v1/tenants/{o['tenant_id']}/audit/cross-case-scan",
            json={},
            headers=_headers(o["token"]),
            timeout=120.0,
        )
        data = _ok_data(resp, "cross-case-scan")
    verdict = data.get("verdict", "?")
    dupes   = data.get("duplicatesFound", 0)
    colour  = "green" if verdict == "PASS" else "red"
    console.print(
        f"[{colour}]✓ Cross-case scan:[/{colour}] "
        f"verdict=[bold]{verdict}[/bold]  duplicates=[bold]{dupes}[/bold]"
    )
    if dupes:
        run_id = data.get("auditRunId", "")
        console.print(f"  Run ID: {run_id}")
        console.print("  Use [cyan]findings list[/cyan] to see cross-case findings.")


# ── findings group ────────────────────────────────────────────────────────────

@cli.group()
def findings() -> None:
    """Query stored findings."""


@findings.command("list")
@click.option("--subject-id", default=None, help="Filter by subject UUID")
@click.option("--severity",   default=None, type=click.Choice(["CRITICAL", "WARNING", "INFO"]),
              help="Filter by severity")
@click.option("--result",     default=None, type=click.Choice(["FAIL", "PASS", "SKIPPED"]),
              help="Filter by result")
@click.pass_context
def findings_list(
    ctx: click.Context,
    subject_id: str | None,
    severity: str | None,
    result: str | None,
) -> None:
    """List stored audit findings."""
    o = ctx.obj
    base = f"{o['audit_url']}/v1/tenants/{o['tenant_id']}"

    params: dict[str, str] = {}
    if severity:
        params["severity"] = severity
    if result:
        params["result"] = result

    sid = subject_id or o.get("subject_id")
    if sid:
        url = f"{base}/subjects/{sid}/audit/findings"
    else:
        url = f"{base}/audit/findings"

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params, headers=_headers(o["token"]))
        data = _ok_data(resp, "findings:list")

    rows = data.get("findings", [])
    if not rows:
        console.print("[dim]No findings found.[/dim]")
        return

    table = Table(
        title=f"Findings ({len(rows)} total)",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold white on dark_blue",
    )
    table.add_column("Rule",     style="cyan",  no_wrap=True)
    table.add_column("Severity", no_wrap=True)
    table.add_column("Result",   no_wrap=True)
    table.add_column("Ack State", no_wrap=True)
    table.add_column("Detail")

    sev_colours = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "blue"}
    res_colours = {"FAIL": "red", "PASS": "green", "SKIPPED": "dim"}
    for row in rows:
        sev = row.get("severity", "?")
        res = row.get("result", "?")
        table.add_row(
            row.get("rule_code") or row.get("ruleCode", ""),
            f"[{sev_colours.get(sev, 'white')}]{sev}[/{sev_colours.get(sev, 'white')}]",
            f"[{res_colours.get(res, 'white')}]{res}[/{res_colours.get(res, 'white')}]",
            row.get("acknowledgement_state") or row.get("ackState", "PENDING"),
            row.get("detail", ""),
        )
    console.print(table)


@findings.command("pending")
@click.option("--severity", default=None, type=click.Choice(["CRITICAL", "WARNING"]),
              help="Filter by severity")
@click.pass_context
def findings_pending(ctx: click.Context, severity: str | None) -> None:
    """Show unacknowledged CRITICAL / WARNING findings (require action)."""
    o = ctx.obj
    url = f"{o['audit_url']}/v1/tenants/{o['tenant_id']}/audit/pending-acknowledgements"
    params: dict[str, str] = {}
    if severity:
        params["severity"] = severity
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params, headers=_headers(o["token"]))
        data = _ok_data(resp, "findings:pending")
    rows = data.get("pending", [])
    if not rows:
        console.print("[green]✓ No pending acknowledgements.[/green]")
        return
    console.print(f"[red]⚠ {len(rows)} finding(s) require acknowledgement:[/red]")
    for row in rows:
        console.print(
            f"  [{row.get('severity','?')}] {row.get('rule_code') or row.get('ruleCode','?')}: "
            f"{row.get('detail','')}"
        )


# ── rules group ───────────────────────────────────────────────────────────────

@cli.group()
def rules() -> None:
    """Query rule configuration."""


@rules.command("list")
@click.option("--category", default=None, help="Filter by category name (substring match)")
@click.option("--enabled-only", is_flag=True, default=False, help="Show only enabled rules")
@click.pass_context
def rules_list(ctx: click.Context, category: str | None, enabled_only: bool) -> None:
    """List all audit rules."""
    o = ctx.obj
    url = f"{o['audit_url']}/v1/tenants/{o['tenant_id']}/audit/rules"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_headers(o["token"]))
        data = _ok_data(resp, "rules:list")
    rows = data.get("rules", [])
    if enabled_only:
        rows = [r for r in rows if r.get("enabled", True)]
    if category:
        rows = [r for r in rows if category.lower() in (r.get("category") or "").lower()]
    if not rows:
        console.print("[dim]No rules match the filter.[/dim]")
        return

    table = Table(
        title=f"Audit Rules ({len(rows)} shown)",
        box=box.ROUNDED,
        show_lines=False,
        header_style="bold white on dark_blue",
    )
    table.add_column("Code",       style="cyan",  no_wrap=True)
    table.add_column("Category",   style="dim")
    table.add_column("Severity",   no_wrap=True)
    table.add_column("Phases",     style="dim",   no_wrap=True)
    table.add_column("Comparator", no_wrap=True)
    table.add_column("Enabled",    no_wrap=True)

    sev_colours = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "blue"}
    for row in rows:
        sev = row.get("severity", "?")
        enabled = row.get("enabled", True)
        phases  = ", ".join(row.get("phases") or []) if isinstance(row.get("phases"), list) \
                  else str(row.get("phases", ""))
        table.add_row(
            row.get("rule_code") or row.get("ruleCode", ""),
            row.get("category", ""),
            f"[{sev_colours.get(sev, 'white')}]{sev}[/{sev_colours.get(sev, 'white')}]",
            phases,
            row.get("comparator", ""),
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )
    console.print(table)


if __name__ == "__main__":
    cli()
