# Audit Test Client

A command-line tool that exercises the full Verigence pipeline end-to-end:

```
DI document upload → DI extraction → DI confirm → Rule Engine audit → findings
```

---

## Prerequisites

```bash
cd audit-test-client
pip install -r requirements.txt
```

---

## Required inputs

| Input | How to get it |
|---|---|
| `TENANT_ID` | Your tenant UUID in Verigence |
| `DI_BASE_URL` | DI service URL, e.g. `https://verigence-di.up.railway.app` |
| `AUDIT_BASE_URL` | Rule engine URL, e.g. `https://audit-api.up.railway.app` |
| `AUTH_TOKEN` | Bearer JWT — see "Auth tokens" below |
| Document files | PDF or image files — one per document type |

### Auth tokens

**Dev / local (mock JWT):**
```
mock.<tenantId>.<actorId>.TENANT_ADMIN
```
Example: `mock.tenant-abc.user-1.TENANT_ADMIN`

**Production:** obtain a real JWT from the Verigence Security module.

---

## Quick start — single command

```bash
python client.py \
  --di-url   https://verigence-di.up.railway.app \
  --audit-url https://audit-api.up.railway.app \
  --tenant-id <TENANT_ID> \
  --token     "mock.<TENANT_ID>.user-1.TENANT_ADMIN" \
  --doc       booking_docket:./samples/booking_docket.pdf \
  --doc       tax_invoice_dms:./samples/invoice.pdf \
  --doc       form_29_30:./samples/form_29_30.pdf \
  audit full
```

The tool will:
1. Create a new subject (auto-generated UUID unless `--subject-id` is given)
2. Upload each `--doc` file to DI under that subject
3. Poll until DI processing is complete
4. Confirm each document
5. Call the rule engine audit endpoint
6. Print a colour-coded findings table

---

## Commands

### `audit full` — run all 85 rules
```bash
python client.py [OPTIONS] audit full
```

### `audit phase` — run a single phase
```bash
python client.py [OPTIONS] audit phase --phase booking
python client.py [OPTIONS] audit phase --phase delivery
python client.py [OPTIONS] audit phase --phase finance
python client.py [OPTIONS] audit phase --phase exchange
python client.py [OPTIONS] audit phase --phase corporate
```

### `audit cross-case` — run cross-case duplicate scan (tenant-wide)
```bash
python client.py [OPTIONS] audit cross-case
```
Note: cross-case does not upload documents — it scans all existing confirmed subjects.

### `findings list` — fetch stored findings without re-running
```bash
python client.py [OPTIONS] findings list
python client.py [OPTIONS] findings list --subject-id <UUID>
```

### `findings pending` — show unacknowledged CRITICAL/WARNING findings
```bash
python client.py [OPTIONS] findings pending
```

### `rules list` — show all 85 rules
```bash
python client.py [OPTIONS] rules list
```

---

## Skip DI upload (audit an existing subject)

If documents are already uploaded and confirmed in DI, skip straight to the audit:

```bash
python client.py \
  --di-url   https://verigence-di.up.railway.app \
  --audit-url https://audit-api.up.railway.app \
  --tenant-id <TENANT_ID> \
  --token     "mock.<TENANT_ID>.user-1.TENANT_ADMIN" \
  --subject-id <EXISTING_SUBJECT_UUID> \
  --skip-upload \
  audit full
```

---

## Document type keys

Use these exact keys with `--doc <key>:<path>`:

| Key | Document |
|---|---|
| `booking_docket` | Booking docket / order form |
| `tax_invoice_dms` | Tax invoice (DMS copy) |
| `tax_invoice_tally` | Tax invoice (Tally copy) |
| `form_29_30` | Form 29 & 30 (RTO transfer) |
| `insurance_certificate` | Insurance certificate |
| `hypothecation_letter` | Hypothecation / finance letter |
| `exchange_valuation` | Exchange vehicle valuation |
| `pan_card` | Customer PAN card |
| `aadhaar_card` | Customer Aadhaar card |
| `ndc_certificate` | NDC / No Due Certificate |
| `delivery_note` | Delivery note / gate pass |
| `debit_note` | Debit note |

You can pass as many or as few as you have — rules that need a missing document will be `SKIPPED`.

---

## Environment variables

All CLI flags can also be set via environment variables:

```bash
export AUDIT_DI_URL=https://verigence-di.up.railway.app
export AUDIT_URL=https://audit-api.up.railway.app
export AUDIT_TENANT_ID=<TENANT_ID>
export AUDIT_TOKEN="mock.<TENANT_ID>.user-1.TENANT_ADMIN"
```

---

## Example output

```
✓ Subject created:   3f2a1b4c-...
✓ Uploaded:          booking_docket  →  doc-id-001
✓ Uploaded:          tax_invoice_dms →  doc-id-002
⏳ Polling DI status: 2/10 attempts...
✓ Confirmed:         doc-id-001
✓ Confirmed:         doc-id-002
✓ Audit complete:    verdict=FAIL  rules=85  pass=71  fail=8  skipped=6

┌──────────────────────────────────────────────────────────────────┐
│                        Audit Findings                            │
├──────────┬──────────┬───────────────┬──────────────────────────┤
│ RuleCode │ Severity │ Category      │ Detail                   │
├──────────┼──────────┼───────────────┼──────────────────────────┤
│ P1.01    │ CRITICAL │ Price Chain   │ Booking vs invoice ≠     │
│ D1.01    │ WARNING  │ Discount Chain│ Discount > 3% of OTR     │
└──────────┴──────────┴───────────────┴──────────────────────────┘
```
