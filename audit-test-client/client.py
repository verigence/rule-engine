#!/usr/bin/env python3
"""audit-test-client/client.py

End-to-end test client for the Verigence Price Anomaly Rule Engine.

Real DI pipeline (from job_runner.py + documents.py):
  1. POST /v1/tenants/{tid}/subjects               → create subject
  2. POST /v1/tenants/{tid}/subjects/{sid}/documents → upload file
     - documentTypeKey is a HINT to the AI classifier
     - DI looks it up in tenant_document_types; if unrecognised → ADDITIONAL
       (Gemini never runs, indexed_fields never populated)
  3. Poll GET /v1/tenants/{tid}/subjects/{sid}/documents/{docId}
     until processingStatus == PROCESSED  (the worker auto-sets
     confirmation_status=CONFIRMED at Step 17 of job_runner.py)
  4. POST audit API  → rule engine reads document_search_index

NOTE: There is NO /confirm endpoint in DI.  Confirmation is automatic.

Usage:
    python client.py --help
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any

import click
import httpx
from rich import box
from rich.console import Console
from rich.table import Table

console = Console()

# Valid SubjectType values from domain/enums.py
SUBJECT_TYPES = ["PERSON", "ORGANIZATION", "OTHER"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def _json_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _check(resp: httpx.Response, label: str) -> dict[str, Any]:
    """Exit on non-2xx with a clear error; return parsed JSON body."""
    if resp.status_code >= 400:
        console.print(f"[red]\u2717 {label} failed [HTTP {resp.status_code}]:[/red]")
        try:
            console.print_json(resp.text)
        except Exception:  # noqa: BLE001
            console.print(resp.text)
        sys.exit(1)
    return resp.json()  # type: ignore[return-value]


def _di_data(resp: httpx.Response, label: str) -> Any:
    """Check DI envelope {errorCode, errorMessage, data} and return .data."""
    body = _check(resp, label)
    error_code = body.get("errorCode", "000")
    if error_code != "000":
        console.print(f"[red]\u2717 {label} errorCode={error_code}: {body.get('errorMessage')}[/red]")
        sys.exit(1)
    return body.get("data", body)


def _audit_data(resp: httpx.Response, label: str) -> Any:
    """Check audit envelope {errorCode, errorMessage, data} and return .data."""
    body = _check(resp, label)
    return body.get("data", body)


# ── DI helpers ────────────────────────────────────────────────────────────────

def di_create_subject(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    token: str,
    subject_type: str,
    display_name: str | None,
) -> str:
    """
    POST /v1/tenants/{tid}/subjects
    Body: {subjectType, displayName?}
    Returns: subject_id (UUID string)
    """
    body: dict[str, Any] = {"subjectType": subject_type}
    if display_name:
        body["displayName"] = display_name

    resp = client.post(
        f"{di_url}/v1/tenants/{tenant_id}/subjects",
        json=body,
        headers=_json_headers(token),
        timeout=15.0,
    )
    data = _di_data(resp, "create_subject")
    sid = str(data.get("subjectId", ""))
    console.print(f"[green]\u2713 Subject created:[/green]   {sid}  (type={subject_type})")
    return sid


def di_upload_document(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    subject_id: str,
    doc_type_key: str,
    file_path: Path,
    token: str,
) -> str:
    """
    POST /v1/tenants/{tid}/subjects/{sid}/documents
    Multipart: file (binary) + documentTypeKey (form field, optional hint)

    documentTypeKey is a HINT to DI's AI classifier.  If the key is not
    registered in tenant_document_types, DI treats the document as ADDITIONAL
    (requires_processing=False) and Gemini never runs — indexed_fields stays
    empty.  The rule engine will SKIP every rule that needs that document.

    Returns: document_id (UUID string)
    """
    with file_path.open("rb") as fh:
        resp = client.post(
            f"{di_url}/v1/tenants/{tenant_id}/subjects/{subject_id}/documents",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (file_path.name, fh, "application/octet-stream")},
            data={"documentTypeKey": doc_type_key},
            timeout=60.0,
        )

    body = _check(resp, f"upload:{doc_type_key}")
    error_code = body.get("errorCode", "000")
    upload_data = body.get("data") or {}
    doc_id = str(upload_data.get("documentId", "<unknown>"))
    upload_status = upload_data.get("uploadStatus", "?")

    if error_code != "000" or upload_status == "REJECTED":
        console.print(
            f"[red]\u2717 Upload REJECTED:[/red]       {doc_type_key:<30}  "
            f"errorCode={error_code}  {body.get('errorMessage', '')}"
        )
        # Don't exit — caller decides whether to continue
        return ""

    console.print(
        f"[green]\u2713 Uploaded (ACCEPTED):[/green]  {doc_type_key:<30}  docId={doc_id}  "
        f"processing={upload_data.get('processingStatus', '?')}"
    )
    return doc_id


def di_poll_until_processed(
    client: httpx.Client,
    di_url: str,
    tenant_id: str,
    subject_id: str,
    doc_ids: list[str],
    token: str,
    max_attempts: int = 30,
    interval: float = 8.0,
) -> None:
    """
    Poll GET /v1/tenants/{tid}/subjects/{sid}/documents/{docId}
    until processingStatus == PROCESSED.

    DI worker (job_runner.py Step 17) sets:
      processing_status  = PROCESSED
      confirmation_status = CONFIRMED      ← automatic, no API call needed
    then writes to document_search_index (Step 17b).

    PROCESSED is the terminal success state the rule engine cares about.
    FAILED means Gemini couldn't process it — rules will SKIP.
    """
    pending = set(doc_ids)
    for attempt in range(1, max_attempts + 1):
        if not pending:
            break
        still_pending: set[str] = set()
        for doc_id in list(pending):
            resp = client.get(
                f"{di_url}/v1/tenants/{tenant_id}/subjects/{subject_id}/documents/{doc_id}",
                headers=_auth_headers(token),
                timeout=15.0,
            )
            body = _check(resp, f"poll:{doc_id}")
            data = body.get("data") or {}
            proc_status = data.get("processingStatus", "") or ""
            conf_status = data.get("confirmationStatus", "") or ""

            if proc_status == "PROCESSED":
                console.print(
                    f"[green]\u2713 Processing complete:[/green] {doc_id}  "
                    f"confirmation={conf_status}"
                )
            elif proc_status == "FAILED":
                console.print(
                    f"[yellow]\u26a0 Processing FAILED:[/yellow]  {doc_id}  "
                    f"(rules needing this document will be SKIPPED)"
                )
            else:
                still_pending.add(doc_id)

        pending = still_pending
        if pending:
            console.print(
                f"[dim]  Attempt {attempt}/{max_attempts}: "
                f"{len(pending)} document(s) still processing \u2014 "
                f"waiting {interval:.0f}s\u2026[/dim]"
            )
            time.sleep(interval)

    if pending:
        console.print(
            f"[red]\u2717 Timed out waiting for documents: {pending}[/red]\n"
            "  These documents may not have indexed_fields yet.\n"
            "  Rules that depend on them will return SKIPPED."
        )
        # Don't exit — user may still want to run audit against whatever IS processed


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
    """Call the rule engine audit endpoint and return the data payload."""
    base = f"{audit_url}/v1/tenants/{tenant_id}"
    if mode == "full":
        url = f"{base}/subjects/{subject_id}/audit"
    elif mode == "phase" and phase:
        url = f"{base}/subjects/{subject_id}/audit/{phase.lower()}"
    elif mode == "cross-case":
        url = f"{base}/audit/cross-case-scan"
    else:
        raise ValueError(f"Unknown audit mode: {mode!r}")

    resp = client.post(url, json={}, headers=_json_headers(token), timeout=120.0)
    return _audit_data(resp, f"audit:{mode}")  # type: ignore[return-value]


def _print_verdict(data: dict[str, Any]) -> None:
    verdict = data.get("verdict", "?")
    s = data.get("summary", {})
    colour = "green" if verdict == "PASS" else "red"
    console.print(
        f"[{colour}]\u2713 Audit complete:[/{colour}]   "
        f"verdict=[bold]{verdict}[/bold]  "
        f"rules={s.get('rulesEvaluated', '?')}  "
        f"[green]pass={s.get('pass', '?')}[/green]  "
        f"[red]fail={s.get('fail', '?')}[/red]  "
        f"[dim]skipped={s.get('skipped', '?')}[/dim]  "
        f"[red]critical={s.get('critical', 0)}[/red]  "
        f"[yellow]warning={s.get('warning', 0)}[/yellow]"
    )


def _print_findings(anomalies: list[dict[str, Any]]) -> None:
    if not anomalies:
        console.print("[green]\u2713 No anomalies found.[/green]")
        return

    table = Table(
        title=f"Anomalies ({len(anomalies)} found)",
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
            str(a.get("leftValue") or ""),
            str(a.get("rightValue") or ""),
        )
    console.print(table)


# ── Shared upload logic ────────────────────────────────────────────────────────

def _do_upload(
    ctx_obj: dict[str, Any],
) -> str:
    """
    Create subject (or reuse) → upload documents → poll until PROCESSED.
    Returns subject_id.
    """
    di_url       = ctx_obj["di_url"]
    tenant_id    = ctx_obj["tenant_id"]
    token        = ctx_obj["token"]
    docs         = ctx_obj["docs"]
    skip         = ctx_obj["skip_upload"]
    subject_id   = ctx_obj["subject_id"]
    subject_type = ctx_obj["subject_type"]
    display_name = ctx_obj["display_name"]

    if skip:
        if not subject_id:
            console.print("[red]\u2717 --subject-id is required when --skip-upload is set.[/red]")
            sys.exit(1)
        console.print(f"[dim]Skipping DI upload \u2014 using existing subject {subject_id}[/dim]")
        return subject_id

    with httpx.Client(timeout=30.0) as client:
        sid = di_create_subject(
            client, di_url, tenant_id, token,
            subject_type=subject_type,
            display_name=display_name,
        )

        if not docs:
            console.print(
                "[yellow]\u26a0 No --doc files provided.  "
                "All audit rules will be SKIPPED.[/yellow]"
            )
            return sid

        doc_ids: list[str] = []
        for raw in docs:
            if ":" not in raw:
                console.print(f"[red]\u2717 --doc must be KEY:PATH, got: {raw!r}[/red]")
                sys.exit(1)
            doc_type_key, _, path_str = raw.partition(":")
            file_path = Path(path_str.strip())
            if not file_path.exists():
                console.print(f"[red]\u2717 File not found: {file_path}[/red]")
                sys.exit(1)
            doc_id = di_upload_document(
                client, di_url, tenant_id, sid,
                doc_type_key.strip(), file_path, token,
            )
            if doc_id:
                doc_ids.append(doc_id)

        if doc_ids:
            console.print()
            console.print("[dim]Waiting for DI worker to process documents\u2026[/dim]")
            console.print(
                "[dim](Worker runs Gemini classify + extract, "
                "then auto-confirms at Step 17)[/dim]"
            )
            di_poll_until_processed(
                client, di_url, tenant_id, sid, doc_ids, token
            )
        else:
            console.print("[yellow]\u26a0 All uploads were REJECTED \u2014 audit will SKIP most rules.[/yellow]")

    return sid


# ── CLI root ──────────────────────────────────────────────────────────────────

@click.group()
@click.option("--di-url",      envvar="AUDIT_DI_URL",    required=True,
              help="DI base URL, e.g. https://verigence-di.up.railway.app")
@click.option("--audit-url",   envvar="AUDIT_URL",        required=True,
              help="Audit service base URL")
@click.option("--tenant-id",   envvar="AUDIT_TENANT_ID",  required=True,
              help="Tenant UUID")
@click.option("--token",       envvar="AUDIT_TOKEN",      required=True,
              help="Bearer JWT. Dev mock: mock.<tenantId>.<actorId>.TENANT_ADMIN")
@click.option("--subject-id",  envvar="AUDIT_SUBJECT_ID", default=None,
              help="Existing subject UUID (required when --skip-upload)")
@click.option("--subject-type", default="PERSON",
              type=click.Choice(SUBJECT_TYPES, case_sensitive=False),
              show_default=True,
              help="SubjectType for new subject creation")
@click.option("--display-name", default=None,
              help="displayName for new subject creation (optional)")
@click.option("--doc", "docs", multiple=True, metavar="KEY:PATH",
              help=(
                  "Document to upload as KEY:PATH.  KEY is a documentTypeKey registered "
                  "in tenant_document_types (e.g. booking_docket).  "
                  "Repeat for multiple documents."
              ))
@click.option("--skip-upload", is_flag=True, default=False,
              help="Skip DI upload \u2014 audit an already-processed subject directly.")
@click.pass_context
def cli(
    ctx: click.Context,
    di_url: str, audit_url: str, tenant_id: str, token: str,
    subject_id: str | None, subject_type: str, display_name: str | None,
    docs: tuple[str, ...], skip_upload: bool,
) -> None:
    """Verigence Audit Test Client \u2014 end-to-end pipeline tester."""
    ctx.ensure_object(dict)
    ctx.obj.update({
        "di_url":       di_url.rstrip("/"),
        "audit_url":    audit_url.rstrip("/"),
        "tenant_id":    tenant_id,
        "token":        token,
        "subject_id":   subject_id,
        "subject_type": subject_type.upper(),
        "display_name": display_name,
        "docs":         docs,
        "skip_upload":  skip_upload,
    })


# ── audit commands ────────────────────────────────────────────────────────────

@cli.group()
def audit() -> None:
    """Run audit rules against a subject."""


@audit.command("full")
@click.pass_context
def audit_full(ctx: click.Context) -> None:
    """Run all 85 rules (full within-case audit)."""
    o = ctx.obj
    sid = _do_upload(o)
    console.print()
    with httpx.Client(timeout=120.0) as client:
        data = run_audit(client, o["audit_url"], o["tenant_id"], sid, o["token"], mode="full")
    _print_verdict(data)
    _print_findings(data.get("anomalies", []))


@audit.command("phase")
@click.option("--phase", required=True,
              type=click.Choice(["booking", "delivery", "finance", "exchange", "corporate"],
                                case_sensitive=False),
              help="Process phase to audit.")
@click.pass_context
def audit_phase(ctx: click.Context, phase: str) -> None:
    """Run rules for a single process phase."""
    o = ctx.obj
    sid = _do_upload(o)
    console.print()
    with httpx.Client(timeout=120.0) as client:
        data = run_audit(client, o["audit_url"], o["tenant_id"], sid, o["token"],
                         mode="phase", phase=phase)
    _print_verdict(data)
    _print_findings(data.get("anomalies", []))


@audit.command("cross-case")
@click.pass_context
def audit_cross_case(ctx: click.Context) -> None:
    """Run cross-case duplicate scan (tenant-wide, no subject needed)."""
    o = ctx.obj
    if not o["skip_upload"] and o["docs"]:
        _do_upload(o)   # upload first if files were provided
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{o['audit_url']}/v1/tenants/{o['tenant_id']}/audit/cross-case-scan",
            json={}, headers=_json_headers(o["token"]), timeout=120.0,
        )
        data = _audit_data(resp, "cross-case-scan")
    verdict = data.get("verdict", "?")
    dupes   = data.get("duplicatesFound", 0)
    colour  = "green" if verdict == "PASS" else "red"
    console.print(
        f"[{colour}]\u2713 Cross-case scan:[/{colour}] "
        f"verdict=[bold]{verdict}[/bold]  "
        f"duplicates=[bold]{dupes}[/bold]"
    )
    if dupes:
        console.print(f"  Run ID: {data.get('auditRunId', '')}")
        console.print("  Use [cyan]findings list[/cyan] to view cross-case findings.")


# ── findings commands ─────────────────────────────────────────────────────────

@cli.group()
def findings() -> None:
    """Query stored audit findings."""


@findings.command("list")
@click.option("--subject-id", default=None, help="Filter by subject UUID")
@click.option("--severity", default=None,
              type=click.Choice(["CRITICAL", "WARNING", "INFO"]),
              help="Filter by severity")
@click.option("--result", default=None,
              type=click.Choice(["FAIL", "PASS", "SKIPPED"]),
              help="Filter by result")
@click.pass_context
def findings_list(
    ctx: click.Context,
    subject_id: str | None, severity: str | None, result: str | None,
) -> None:
    """List stored audit findings."""
    o = ctx.obj
    base = f"{o['audit_url']}/v1/tenants/{o['tenant_id']}"
    sid  = subject_id or o.get("subject_id")
    url  = f"{base}/subjects/{sid}/audit/findings" if sid else f"{base}/audit/findings"

    params: dict[str, str] = {}
    if severity: params["severity"] = severity
    if result:   params["result"]   = result

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params, headers=_auth_headers(o["token"]))
        data = _audit_data(resp, "findings:list")

    rows = data.get("findings", [])
    if not rows:
        console.print("[dim]No findings found.[/dim]")
        return

    table = Table(
        title=f"Findings ({len(rows)} total)",
        box=box.ROUNDED, show_lines=True,
        header_style="bold white on dark_blue",
    )
    table.add_column("Rule",      style="cyan",  no_wrap=True)
    table.add_column("Severity",  no_wrap=True)
    table.add_column("Result",    no_wrap=True)
    table.add_column("Ack State", no_wrap=True)
    table.add_column("Detail")

    sev_c = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "blue"}
    res_c = {"FAIL": "red", "PASS": "green", "SKIPPED": "dim"}
    for row in rows:
        sev = row.get("severity", "?")
        res = row.get("result", "?")
        table.add_row(
            row.get("rule_code") or row.get("ruleCode", ""),
            f"[{sev_c.get(sev,'white')}]{sev}[/{sev_c.get(sev,'white')}]",
            f"[{res_c.get(res,'white')}]{res}[/{res_c.get(res,'white')}]",
            row.get("acknowledgement_state") or "PENDING",
            row.get("detail", ""),
        )
    console.print(table)


@findings.command("pending")
@click.option("--severity", default=None,
              type=click.Choice(["CRITICAL", "WARNING"]),
              help="Filter by severity")
@click.pass_context
def findings_pending(ctx: click.Context, severity: str | None) -> None:
    """Show unacknowledged CRITICAL/WARNING findings that need action."""
    o = ctx.obj
    url = f"{o['audit_url']}/v1/tenants/{o['tenant_id']}/audit/pending-acknowledgements"
    params: dict[str, str] = {}
    if severity: params["severity"] = severity
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, params=params, headers=_auth_headers(o["token"]))
        data = _audit_data(resp, "findings:pending")
    rows = data.get("pending", [])
    if not rows:
        console.print("[green]\u2713 No pending acknowledgements.[/green]")
        return
    console.print(f"[red]\u26a0 {len(rows)} finding(s) require acknowledgement:[/red]")
    for row in rows:
        console.print(
            f"  [{row.get('severity','?')}] "
            f"{row.get('rule_code') or row.get('ruleCode','?')}: "
            f"{row.get('detail','')}"
        )


# ── rules commands ────────────────────────────────────────────────────────────

@cli.group()
def rules() -> None:
    """Query rule configuration."""


@rules.command("list")
@click.option("--category", default=None, help="Filter by category name (substring)")
@click.option("--enabled-only", is_flag=True, default=False)
@click.pass_context
def rules_list(ctx: click.Context, category: str | None, enabled_only: bool) -> None:
    """List all 85 audit rules."""
    o = ctx.obj
    url = f"{o['audit_url']}/v1/tenants/{o['tenant_id']}/audit/rules"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=_auth_headers(o["token"]))
        data = _audit_data(resp, "rules:list")
    rows = data.get("rules", [])
    if enabled_only: rows = [r for r in rows if r.get("enabled", True)]
    if category:     rows = [r for r in rows if category.lower() in (r.get("category") or "").lower()]
    if not rows:
        console.print("[dim]No rules match the filter.[/dim]")
        return

    table = Table(
        title=f"Audit Rules ({len(rows)} shown)",
        box=box.ROUNDED, show_lines=False,
        header_style="bold white on dark_blue",
    )
    table.add_column("Code",       style="cyan",  no_wrap=True)
    table.add_column("Category",   style="dim")
    table.add_column("Severity",   no_wrap=True)
    table.add_column("Phases",     style="dim",   no_wrap=True)
    table.add_column("Comparator", no_wrap=True)
    table.add_column("On",         no_wrap=True)

    sev_c = {"CRITICAL": "red", "WARNING": "yellow", "INFO": "blue"}
    for row in rows:
        sev = row.get("severity", "?")
        enabled = row.get("enabled", True)
        phases  = (
            ", ".join(row["phases"]) if isinstance(row.get("phases"), list)
            else str(row.get("phases", ""))
        )
        table.add_row(
            row.get("rule_code") or row.get("ruleCode", ""),
            row.get("category", ""),
            f"[{sev_c.get(sev,'white')}]{sev}[/{sev_c.get(sev,'white')}]",
            phases,
            row.get("comparator", ""),
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )
    console.print(table)


if __name__ == "__main__":
    cli()
