# Rule → DI / Audit Core operand crosswalk

**Status:** review reference for the operand re-pointing migration (`0002`).
**Date:** 2026-09-07

The 85 rules in `0001_audit_engine.py` were seeded against the
`PRICE_ANOMALY_RULE_ENGINE.md` v5.1 vocabulary, which predates DI's v2
`SCHEMA_REGISTRY` and diverges from it at nearly every operand — document-type
name **and** field key. This document maps every rule to what DI / Audit Core
actually produces today.

## 1. Document-type map

| Rule engine key | Real source | Notes |
|---|---|---|
| `tax_invoice_dms` | `customer_invoice_dms` | + field remap |
| `tax_invoice_tally` | `tax_invoice_tally` | + field remap |
| `booking_docket` | `booking_form` | + field remap; no `chassis_number` (pre-VIN) |
| `kyc_pan` | `pan_card` | |
| `kyc_aadhaar` | `aadhaar` | `name`→`aadhaar_name`; no `expiry_date` |
| `insurance_cover_note` | `insurance_cover` | `start_date`→`policy_start_date` |
| `trade_in_valuation` | `valuation_report` | `assessed_value`→`final_offer_value` |
| `delivery_order` | `delivery_order_cover` | `do_date`→`delivery_date` |
| `gate_pass` | `gate_pass` | `gate_date`→`delivery_date`; **no `chassis_number`** (only `vehicle_registration_number`) |
| `bank_approval_letter` | `bank_approval_letter` | `loan_amount`→`sanctioned_amount` |
| `gst_certificate` | `gst_certificate` | `company_name`→`legal_name` |
| `rto_challan` | `rto_challan` | **no total RTO fee field** — challan schema is `registration_number / state / territory / district / ex_showroom_amount / registration_type / hp_charges_amount` |
| `accessory_invoice_dms` / `_tally` | same | `total_amount`→`grand_total_amount`; line items carry `line_category` |
| `customer_ledger` | `customer_ledger` | **added in DI step 2** — `total_debited / total_credited / closing_balance / cash_credit_total / loan_credit` |
| `debit_note` | `debit_note` | **added in DI step 2** — `insurance_amount / rto_amount / total_amount` |
| `purchase_order` | `purchase_order` | **added in DI step 2** — `po_amount / authoriser_signature_present` |
| `payment_receipt_tally` | *(no such doc — Tally emits invoices)* | payments come from `dealer_receipt` / `upi_transaction` / `upi_screenshot` / `bank_statement_extract` → `_derived / payments_total` |
| `cost_sheet` | *(price master, not a document)* | → `_reconciliation` MASTER operands (`auditcore.commercial_lines.standard_amount` etc.) |
| `discount_approval_form` | *(discount master, not a document)* | → `_reconciliation` MASTER (`auditcore.discount_applications.standard_eligible_amount`) |
| `registration_certificate` | *(not extracted)* | **PARK** — no RC schema in DI |
| `trade_in_rc` | *(not extracted)* | **PARK** |
| `customer_id_delivery` | *(not modelled — one KYC per journey)* | **PARK** |
| `ndc` | *(No Dues Certificate — not extracted)* | **PARK** |
| `third_party_declaration` | *(not extracted)* | **PARK** |

## 2. Field-key map (per renamed doc type)

**`booking_form`** (was `booking_docket`)

| old field | new field |
|---|---|
| `agreed_price` | `total_price` |
| `discount_promised` | `discount_amount` |
| `accessories_promised` | `accessories_cost` |
| `model_variant` | `vehicle_variant` |
| `customer_name`, `booking_date`, `booking_amount_paid` | unchanged |
| `chassis_number` | — (not on a booking form) |

**`customer_invoice_dms`** (was `tax_invoice_dms`)

| old field | new field |
|---|---|
| `net_payable` | `grand_total_amount` |
| `customer_name` | `buyer_name` |
| `discount_amount` | `invoice_discount_amount` |
| `model_variant` | `variant_raw` |
| `taxable_amount`, `invoice_date`, `buyer_gstin`, `chassis_number` | unchanged |
| `finance_amount` | — (only `financed_by` name) → use `bank_approval_letter.sanctioned_amount` |
| `outstanding` | — not extracted |
| `exchange_credit` | — → `_reconciliation / discount:EXCHANGE:actual` |
| `delivery_date` | — not on invoice → use `gate_pass.delivery_date` |
| `gst_amount` | — → sum of `cgst_amount + sgst_amount + igst_amount` (needs a derived; PARK for now) |

**`tax_invoice_tally`**: `net_payable`→`grand_total_amount`, `discount_amount`→`invoice_discount_amount`.

**Others**: `aadhaar.name`→`aadhaar_name`; `insurance_cover.start_date`→`policy_start_date`;
`valuation_report.assessed_value`→`final_offer_value`; `delivery_order_cover.do_date`→`delivery_date`;
`gate_pass.gate_date`→`delivery_date`; `bank_approval_letter.loan_amount`→`sanctioned_amount`;
`gst_certificate.company_name`→`legal_name`; `accessory_invoice_*.total_amount`→`grand_total_amount`.

## 3. New operand sentinels (rule-engine step 4)

| `left_doc_type` | `left_field_key` | resolves to |
|---|---|---|
| `_reconciliation` | `discount:<KEY>:standard` / `:actual` | `auditcore.discount_applications` |
| `_reconciliation` | `commercial:<component_key>:standard` / `:actual` | `auditcore.commercial_lines` |
| `_reconciliation` | `addon:<TYPE>:standard` / `:actual` | `auditcore.journey_addons` |
| `_derived` | `payments_total` | Σ payment-evidence amounts |
| `_derived` | `lineitem:<CATEGORY>` | Σ invoice `line_items[]` net where `line_category` matches |

Rules using a sentinel operand **must** have `requires_both_docs = false`.

## 4. Per-rule disposition

Legend — **RENAME**: doc rename only · **REMAP**: rename + field-key change ·
**MASTER** / **DERIVED**: operand becomes a sentinel · **PARK**: `enabled=false`,
no DI source today.

| Rule | Disposition | New left operand | New right operand |
|---|---|---|---|
| PRICE_BOOKING_VS_INVOICE | REMAP | `booking_form.total_price` | `customer_invoice_dms.grand_total_amount` |
| PRICE_INVOICE_DMS_VS_TALLY | REMAP | `customer_invoice_dms.grand_total_amount` | `tax_invoice_tally.grand_total_amount` |
| PRICE_INVOICE_VS_LEDGER | REMAP | `customer_invoice_dms.grand_total_amount` | `customer_ledger.total_debited` |
| PRICE_LEDGER_VS_DMS | PARK | `customer_ledger.closing_balance` | invoice `outstanding` not extracted |
| PAYMENT_SUM_VS_INVOICE | DERIVED | `_derived.payments_total` | `customer_invoice_dms.grand_total_amount` |
| PAYMENT_SUM_VS_LEDGER | DERIVED | `_derived.payments_total` | `customer_ledger.total_credited` |
| LOAN_VS_INVOICE_FINANCE | PARK | `bank_approval_letter.sanctioned_amount` | invoice `finance_amount` not extracted |
| LOAN_VS_LEDGER_CREDIT | REMAP | `bank_approval_letter.sanctioned_amount` | `customer_ledger.loan_credit` |
| COST_SHEET_TOTAL_VS_INVOICE | PARK | cost-sheet total = Σ master components (needs a derived) | — |
| DISCOUNT_EXCEEDS_POLICY | MASTER | `_reconciliation.discount:TOTAL:actual` | `cfg:config.region_max_discount` |
| DISCOUNT_BOOKING_EXCEEDS_APPROVAL | MASTER | `booking_form.discount_amount` | `_reconciliation.discount:TOTAL:standard` |
| DISCOUNT_INVOICE_VS_BOOKING | REMAP | `customer_invoice_dms.invoice_discount_amount` | `booking_form.discount_amount` |
| DISCOUNT_INVOICE_DMS_VS_TALLY | REMAP | `customer_invoice_dms.invoice_discount_amount` | `tax_invoice_tally.invoice_discount_amount` |
| DISCOUNT_APPROVAL_MISSING | MASTER | `customer_invoice_dms.invoice_discount_amount` | (GT 0 & no `discount:*:standard`) |
| DISCOUNT_APPROVAL_UNSIGNED | PARK | approval-form signature not extracted (master-based has no signature) | — |
| DISCOUNT_HIDDEN_IN_EXCHANGE | MASTER | `customer_invoice_dms.invoice_discount_amount` | `_reconciliation.discount:TOTAL:standard` |
| ACCESSORY_DMS_VS_TALLY | REMAP | `accessory_invoice_dms.grand_total_amount` | `accessory_invoice_tally.grand_total_amount` |
| ACCESSORY_VS_COST_SHEET | MASTER | `accessory_invoice_dms.grand_total_amount` | `_reconciliation.addon:ACCESSORIES_TOTAL:standard` |
| ACCESSORY_VIN_VS_VEHICLE_VIN | REMAP | `accessory_invoice_dms.chassis_number` | `customer_invoice_dms.chassis_number` |
| ACCESSORY_DATE_VS_DELIVERY | REMAP | `accessory_invoice_dms.invoice_date` | `gate_pass.delivery_date` |
| ACCESSORY_BOOKING_VS_INVOICE | REMAP | `booking_form.accessories_cost` | `accessory_invoice_dms.grand_total_amount` |
| INSURANCE_PREMIUM_COVER_VS_COST_SHEET | MASTER | `insurance_cover.premium_amount` | `_reconciliation.commercial:insurance_amount:standard` |
| INSURANCE_PREMIUM_VS_DEBIT_NOTE | REMAP | `insurance_cover.premium_amount` | `debit_note.insurance_amount` |
| INSURANCE_COVER_NOTE_MISSING | RENAME | presence of `insurance_cover` | — |
| INSURANCE_START_AFTER_DELIVERY | REMAP | `insurance_cover.policy_start_date` | `gate_pass.delivery_date` |
| RTO_COST_SHEET_VS_CHALLAN | PARK | RTO challan has no total-fee field | — |
| RTO_DEBIT_NOTE_VS_CHALLAN | PARK | RTO challan has no total-fee field | — |
| RTO_CHALLAN_MISSING | RENAME | presence of `rto_challan` | — |
| CHASSIS_BOOKING_VS_INVOICE | PARK | booking form is pre-VIN | — |
| CHASSIS_INVOICE_VS_RC | PARK | no RC schema | — |
| CHASSIS_RC_VS_INSURANCE | PARK | no RC schema | — |
| CHASSIS_INVOICE_VS_GATE | PARK | gate pass has no `chassis_number` | — |
| CHASSIS_INVOICE_VS_DO | REMAP | `customer_invoice_dms.chassis_number` | `delivery_order_cover.chassis_number` |
| MODEL_VARIANT_BOOKING_VS_INVOICE | REMAP | `booking_form.vehicle_variant` | `customer_invoice_dms.variant_raw` |
| CUSTOMER_NAME_BOOKING_VS_INVOICE | REMAP | `booking_form.customer_name` | `customer_invoice_dms.buyer_name` |
| PAYMENT_DATE_BEFORE_BOOKING | PARK | needs `_derived.payments_earliest_date` | `booking_form.booking_date` |
| INVOICE_DATE_BEFORE_BOOKING | REMAP | `customer_invoice_dms.invoice_date` | `booking_form.booking_date` |
| GATE_DATE_BEFORE_INVOICE | REMAP | `gate_pass.delivery_date` | `customer_invoice_dms.invoice_date` |
| NDC_DATE_AFTER_GATE | PARK | no NDC schema | — |
| DO_DATE_AFTER_GATE | REMAP | `delivery_order_cover.delivery_date` | `gate_pass.delivery_date` |
| RC_DELAY_EXCESSIVE | PARK | no RC schema | — |
| BOOKING_TO_DELIVERY_EXCESS | REMAP | `gate_pass.delivery_date` | `booking_form.booking_date` |
| DMS_DELIVERY_DATE_VS_GATE | PARK | invoice has no `delivery_date` | `gate_pass.delivery_date` |
| EXCHANGE_VALUE_BELOW_MARKET | REMAP | `valuation_report.final_offer_value` | `cfg:config.market_floor_ratio` (× `valuation_report.base_market_value`) |
| EXCHANGE_VALUE_VS_INVOICE_CREDIT | MASTER | `valuation_report.final_offer_value` | `_reconciliation.discount:EXCHANGE:actual` |
| EXCHANGE_VALUE_VS_COST_SHEET | MASTER | `valuation_report.final_offer_value` | `_reconciliation.discount:EXCHANGE:standard` |
| EXCHANGE_RC_OWNER_VS_KYC | PARK | no trade-in RC schema | — |
| EXCHANGE_WITHOUT_RC | PARK | no trade-in RC schema | — |
| KYC_NAME_VS_BOOKING | REMAP | `aadhaar.aadhaar_name` | `booking_form.customer_name` |
| KYC_NAME_VS_INVOICE | REMAP | `aadhaar.aadhaar_name` | `customer_invoice_dms.buyer_name` |
| KYC_NAME_VS_RC | PARK | no RC schema | — |
| KYC_DOB_AADHAAR_VS_PAN | REMAP | `aadhaar.date_of_birth` | `pan_card.date_of_birth` |
| KYC_AADHAAR_EXPIRED | PARK | Aadhaar does not expire / no `expiry_date` | — |
| LOAN_APPLICANT_VS_KYC | REMAP | `bank_approval_letter.applicant_name` | `aadhaar.aadhaar_name` |
| DELIVERY_KYC_VS_BOOKING_KYC | PARK | one KYC per journey — no delivery-time re-capture | — |
| THIRD_PARTY_DECLARATION_MISSING | PARK | needs `_derived` payer name + no declaration schema | — |
| THIRD_PARTY_AMOUNT_VS_RECEIPT | PARK | no third-party declaration schema | — |
| CASH_ABOVE_2_LAKH | PARK | needs `_derived.payments_max` (per-receipt max) | `cfg:config.cash_limit` |
| CORPORATE_GSTIN_CERT_VS_INVOICE | REMAP | `gst_certificate.gstin` | `customer_invoice_dms.buyer_gstin` |
| CORPORATE_NAME_CERT_VS_INVOICE | REMAP | `gst_certificate.legal_name` | `customer_invoice_dms.buyer_name` |
| CORPORATE_PO_MISSING | RENAME | presence of `purchase_order` | — |
| CORPORATE_PO_AMOUNT_VS_INVOICE | REMAP | `purchase_order.po_amount` | `customer_invoice_dms.grand_total_amount` |
| NDC_MISSING | PARK | no NDC schema | — |
| NDC_CUSTOMER_VS_KYC | PARK | no NDC schema | — |
| BOOKING_DOCKET_MISSING | REMAP | presence of `booking_form` | — |
| INVOICE_MISSING | REMAP | presence of `customer_invoice_dms` | — |
| GATE_PASS_MISSING | RENAME | presence of `gate_pass` | — |
| CUSTOMER_LEDGER_MISSING | RENAME | presence of `customer_ledger` | — |
| COST_SHEET_MISSING | PARK | cost sheet is master, not a document | — |
| GATE_PASS_CHASSIS_EMPTY | REMAP | `gate_pass.vehicle_registration_number` (chassis not on gate pass) | — |
| BOOKING_AMOUNT_ZERO | REMAP | `booking_form.booking_amount_paid` | — |
| INVOICE_GST_ARITHMETIC | PARK | needs `_derived` GST sum (`cgst+sgst+igst`) | `customer_invoice_dms.taxable_amount` |
| DUPLICATE_RECEIPT_NUMBER | PARK | needs `_derived` receipt-number count | — |
| DELIVERY_ORDER_MISSING | REMAP | presence of `delivery_order_cover` | — |
| PAYMENT_MODE_CASH_UNRECEIPTED | DERIVED | `customer_ledger.cash_credit_total` | `_derived.payments_total` |
| TALLY_VEHICLE_INVOICE_VS_DMS | REMAP | `tax_invoice_tally.grand_total_amount` | `customer_invoice_dms.grand_total_amount` |
| TALLY_ACCESSORY_INVOICE_VS_DMS | REMAP | `accessory_invoice_tally.grand_total_amount` | `accessory_invoice_dms.grand_total_amount` |
| DEBIT_NOTE_INSURANCE_VS_COVER_NOTE | REMAP | `debit_note.insurance_amount` | `insurance_cover.premium_amount` |
| DEBIT_NOTE_RTO_VS_CHALLAN | PARK | RTO challan has no total-fee field | — |
| DUPLICATE_PAN_ACROSS_BOOKINGS | RENAME (cross-case) | `pan_card.pan_number` | — |
| DUPLICATE_AADHAAR_ACROSS_BOOKINGS | RENAME (cross-case) | `aadhaar.aadhaar_number` | — |
| DUPLICATE_CHASSIS_ACROSS_INVOICES | REMAP (cross-case) | `customer_invoice_dms.chassis_number` | — |
| DUPLICATE_CHASSIS_ACROSS_GATE_PASSES | PARK (cross-case) | gate pass has no `chassis_number` | — |
| DUPLICATE_RECEIPT_ACROSS_CASES | REMAP (cross-case) | `dealer_receipt.receipt_number` | — |
| DUPLICATE_UTR_ACROSS_CASES | REMAP (cross-case) | `bank_statement_extract.reference_no` / `upi_transaction.upi_rrn` | — |

## 5. Summary (matches `0002` exactly)

| Disposition | Count | Where in `0002` |
|---|---|---|
| REMAP (doc + field re-point) | 32 | `_REMAP` |
| Presence-only `*_MISSING` (doc-type rename inside `condition_expression`) | 8 | `_CONDITION_RENAMES` |
| MASTER + DERIVED (`_reconciliation` / `_derived` sentinels, `requires_both_docs=false`) | 11 | `_SENTINEL` |
| **PARK** (`enabled=false` — no DI source today) | 28 | `_PARK` |
| **Within-case total** | **79** | |
| Cross-case (in `cross_case_engine._CROSS_CASE_RULES`, not the migration) | 6 | 4 remapped, 1 field-swapped, 1 → `dealer_receipt.payment_reference_no` |

## 6. What "PARK" needs to become active

| Gap | Unblocks |
|---|---|
| DI `registration_certificate` schema (`owner_name`, `chassis_number`, `issue_date`) | 5 rules (`CHASSIS_INVOICE_VS_RC`, `CHASSIS_RC_VS_INSURANCE`, `KYC_NAME_VS_RC`, `RC_DELAY_EXCESSIVE`, + trade-in RC) |
| DI `gate_pass.chassis_number` | 2 rules + 1 cross-case |
| DI `rto_challan` total-fee field | 3 rules |
| DI `ndc` (No Dues Certificate) schema | 3 rules |
| DI `third_party_declaration` schema | 2 rules |
| DI invoice `delivery_date` **or** accept `gate_pass.delivery_date` as the delivery date everywhere | 1 rule |
| `_derived` helpers: `payments_max`, `payments_earliest_date`, `receipt_number_count`, `gst_sum` | 5 rules |
| Trade-in RC / delivery-time KYC re-capture in the journey model | 3 rules |
