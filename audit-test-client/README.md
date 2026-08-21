# Audit Test Client

A CLI that exercises the full Verigence pipeline end-to-end:

```
DI: create subject → upload documents → worker auto-processes & confirms
                                              ↓
                                     document_search_index populated
                                              ↓
Rule engine: read index → evaluate 85 rules → findings
```

---

## How the DI pipeline actually works

This is critical to understand before using this tool:

| Step | What happens | Who does it |
|---|---|---|
| `POST /subjects` | Create subject with `subjectType` | Caller |
| `POST /documents` | Upload file + `documentTypeKey` hint | Caller |
| *(async)* | DI worker classifies doc using Gemini | DI worker |
| *(async)* | Gemini extracts fields → `document_search_index` | DI worker |
| *(auto)* | Worker sets `processingStatus=PROCESSED`, `confirmationStatus=CONFIRMED` | DI worker, Step 17 |
| Poll | Wait until `processingStatus == PROCESSED` | Caller |
| Audit | Rule engine reads `document_search_index.indexed_fields` | Rule engine |

**There is no `/confirm` endpoint.** Confirmation happens automatically at the end of the worker pipeline.

### The `documentTypeKey` hint

`documentTypeKey` is passed to DI's AI classifier as a hint. DI checks `tenant_document_types` to see if the key is registered. Two outcomes:

| Key is... | DI does... | indexed_fields |
|---|---|---|
| Registered in `tenant_document_types` with `requires_processing=true` | Gemini runs classification + extraction | Populated ✓ |
| Not registered / unknown | Document type = `ADDITIONAL`, `requires_processing=false` | **Empty** — rules SKIP |

**Ask your DI admin which document type keys are registered for your tenant before running tests.**

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
| `--tenant-id` | Your tenant UUID |
| `--di-url` | DI service URL, e.g. `https://verigence-di.up.railway.app` |
| `--audit-url` | Rule engine URL, e.g. `https://audit-api.up.railway.app` |
| `--token` | Bearer JWT — see below |
| `--doc KEY:FILE` | One or more documents. KEY must be a registered `documentTypeKey` |

### Auth tokens

**Dev / local (mock JWT — DI_ENV != production):**
```
mock.<tenantId>.<actorId>.TENANT_ADMIN
```
Example: `mock.tenant-abc.user-1.TENANT_ADMIN`

**Production:** obtain a real JWT from Verigence Security.

---

## Quick start

```bash
python client.py \
  --di-url    https://verigence-di.up.railway.app \
  --audit-url https://audit-api.up.railway.app \
  --tenant-id <TENANT_UUID> \
  --token     "mock.<TENANT_UUID>.user-1.TENANT_ADMIN" \
  --subject-type PERSON \
  --display-name "Test Customer" \
  --doc booking_docket:./samples/booking.pdf \
  --doc tax_invoice_dms:./samples/invoice.pdf \
  audit full
```

The tool will:
1. Create a new subject (`subjectType=PERSON`)
2. Upload each `--doc` file to DI with the given key as a classifier hint
3. Poll `GET /subjects/{sid}/documents/{docId}` until `processingStatus=PROCESSED`
4. Call the rule engine — `confirmation_status=CONFIRMED` is already set by the worker
5. Print a colour-coded findings table

---

## Commands

### `audit full` — all 85 rules
```bash
python client.py [OPTIONS] audit full
```

### `audit phase` — one process phase
```bash
python client.py [OPTIONS] audit phase --phase booking
python client.py [OPTIONS] audit phase --phase delivery
python client.py [OPTIONS] audit phase --phase finance
python client.py [OPTIONS] audit phase --phase exchange
python client.py [OPTIONS] audit phase --phase corporate
```

### `audit cross-case` — tenant-wide duplicate scan
```bash
python client.py [OPTIONS] audit cross-case
```
No subject or documents needed — scans all confirmed subjects for the tenant.

### `findings list` — query stored findings
```bash
python client.py [OPTIONS] findings list
python client.py [OPTIONS] --subject-id <UUID> findings list
python client.py [OPTIONS] findings list --severity CRITICAL
```

### `findings pending` — unacknowledged CRITICAL/WARNING
```bash
python client.py [OPTIONS] findings pending
```

### `rules list` — all 85 rules
```bash
python client.py [OPTIONS] rules list
python client.py [OPTIONS] rules list --category "Price Chain"
python client.py [OPTIONS] rules list --enabled-only
```

---

## Audit an existing subject (skip upload)

If documents are already uploaded and processed in DI:

```bash
python client.py \
  --di-url    https://verigence-di.up.railway.app \
  --audit-url https://audit-api.up.railway.app \
  --tenant-id <TENANT_UUID> \
  --token     "mock.<TENANT_UUID>.user-1.TENANT_ADMIN" \
  --subject-id <EXISTING_SUBJECT_UUID> \
  --skip-upload \
  audit full
```

---

## Environment variables

All flags can be set via env vars:

```bash
export AUDIT_DI_URL=https://verigence-di.up.railway.app
export AUDIT_URL=https://audit-api.up.railway.app
export AUDIT_TENANT_ID=<TENANT_UUID>
export AUDIT_TOKEN="mock.<TENANT_UUID>.user-1.TENANT_ADMIN"
```

---

## SubjectType values

| Value | Meaning |
|---|---|
| `PERSON` | Individual customer (default) |
| `ORGANIZATION` | Corporate / company |
| `OTHER` | Anything else |

---

## Common `documentTypeKey` values

These must be **registered in your tenant's `tenant_document_types`** table.
Ask your DI admin if unsure. Unregistered keys silently produce empty `indexed_fields`.

| Key | Document |
|---|---|
| `booking_docket` | Booking docket / order form |
| `tax_invoice_dms` | Tax invoice (DMS system copy) |
| `tax_invoice_tally` | Tax invoice (Tally copy) |
| `form_29_30` | Form 29 & 30 (RTO vehicle transfer) |
| `insurance_certificate` | Motor insurance certificate |
| `hypothecation_letter` | Finance / hypothecation letter |
| `exchange_valuation` | Exchange vehicle valuation report |
| `pan_card` | Customer PAN card |
| `aadhaar_card` | Customer Aadhaar card |
| `ndc_certificate` | NDC / No Due Certificate |
| `delivery_note` | Delivery note / gate pass |
| `debit_note` | Debit note |

---

## What "SKIPPED" means in audit results

A rule returns `SKIPPED` when a required field is absent from `indexed_fields`. This happens when:

1. The `documentTypeKey` was not registered in `tenant_document_types` → Gemini never ran
2. The document upload was REJECTED
3. The worker failed (`processingStatus=FAILED`)
4. The field simply wasn't extracted (Gemini returned null)

A high SKIPPED count usually means the `documentTypeKey` values you used aren't registered.
