# Technominds elected ledger: first ERPNext shadow adapter

All applications remain standalone-capable. The owner's2026-10-01 decision elects ERPNext as the default integrated ledger, Akaunting as the Books alternative for entities without ERPNext, and Invoice Ninja as billing/receivables rather than General Ledger authority. Authority belongs to each legal entity, not to an entire workspace. This module is optional and disabled unless configured.

Configure `technominds_ledger` in the native site's private configuration with trusted `tenant_id`, `environment` and an `authorities` list. Each entry specifies `legal_entity_id`, `provider`, `provider_company_id`, `base_currency`, `currency_precision`, `effective_from`, `policy_version`, `posting_mode` and `accounts` (source account key to exact native account name). Providers are `NONE | ERPNEXT | AKAUNTING | EXTERNAL`; posting modes are `DISABLED | SHADOW | LIVE`. Only ERPNext SHADOW is executable here. NONE must be DISABLED. Missing configuration, duplicate entity mappings, duplicate provider-company mappings, future authority, other providers and LIVE all fail closed. Do not infer company/account mappings from names or install ERPNext for a standalone customer.

POST authenticated native methods:

- `erpnext.erpnext_integrations.ledger_shadow.preview_batch`: validate and retain a source-linked shadow proposal in the existing native Integration Request audit store. The receipt has a unique deterministic business identity. Replays recheck current native company/account/journal-create permissions and period/freeze rules before returning; a changed payload under the same source version conflicts. Integration Request persistence is internal and permission bypass is limited to that audit write after domain authorization. No user may use this API to write a voucher.
- `erpnext.erpnext_integrations.ledger_shadow.compare_voucher`: compare that proposal against a specifically named, readable, submitted Sales Invoice, Payment Entry or Journal Entry in the same company/date. Read native GL rows through current permissions, reject incomplete or over-capacity visibility, and compare exact per-account debit/credit totals.

The first source envelope supports `INVOICENINJA` invoice, customer receipt and reversal proposals with decimal-string lines and matching control totals. Native account mappings and company currency are checked. A reversal proposal references a readable submitted Journal Entry in the same company. CRM won events, payment notifications, stock projections, payroll and unimplemented sources are rejected. No statutory rate is inferred.

## Deliberate release boundary

This is a SHADOW adapter, not a posting endpoint. It never inserts/submits/cancels a Journal Entry, Sales Invoice, Payment Entry, GL Entry or Stock Ledger Entry. It does not invoke native submit-and-rollback ledger preview because those paths can run stock operations and hooks. Accounting corrections still require supported native reversal/cancellation/amendment workflows; source history is never overwritten.

`MATCH` means **base-currency account totals match the named voucher**, not full financial reconciliation or source authenticity. Every response has `posted=false` and `live_eligible=false`; comparison responses specify `comparison_scope=BASE_CURRENCY_ACCOUNT_TOTALS`. Party/allocation, dimensions, rounding/FX, source approvals and signed producer transport need subsequent native adapters and reviewed accounting cases. The supplied source hash is a caller claim, not verified Invoice Ninja producer evidence. Shadow receipts are not proof that a legal invoice was issued, payment accepted or revenue recognized.

LIVE remains unavailable even if an operator changes the mode. Required later gates include authentic Invoice Ninja producer/consumer flow, persistent replay/concurrency tests on the target site, hundreds of qualified shadow cases, party/tax/period/dimension/correction reconciliation, effective-dated authority migration and operational rollback. Akaunting's Double-Entry edition/access must be verified before its adapter is built. EXTERNAL is a reserved provider choice, not a connector.

No new DocType, migration, UI, provider credentials, fiscal submission or optional-suite dependency is introduced. Keep `technominds_ledger` absent for standalone operation. Test on an isolated synthetic site and archive native table fingerprints proving shadow calls leave accounting/stock vouchers and balances untouched. Integration Request receipt retention follows source/receipt audit lifetime; deleting receipts must not become a mechanism for retrying a future irreversible effect.

## Request boundary and synthetic test commands

`batch` is a JSON object with exactly: `contract_version` (`C11/ledger-shadow-v1`), `tenant_id`, `legal_entity_id`, `environment`, `source_app` (`INVOICENINJA`), `source_id`, positive integer `source_version`, lowercase SHA-256 `source_hash`, `posting_purpose`, ISO `posting_date`, base `currency`, decimal-string `debit_total`/`credit_total`, and 2–500 `lines`. Each line has `account_key`, `debit`, `credit`; exactly one side is positive. `REVERSAL` additionally requires `reversal_of`. Authority, account resolution, business key, normalized payload hash and policy version are derived on the server. Do not pass the older portfolio fixture schema as an API request.

Amounts must be exact at the configured precision (0–6), at most 1,000,000,000,000 per amount. Floats, scientific notation, negative amounts, implicit rounding and mixed currency are rejected. Native fiscal-year, closed-accounting-period, frozen-date and period-closing-voucher gates are checked. These validations do not replace native party/tax/dimension posting rules, which this proposal does not claim to execute.

On a disposable, isolated native bench with ERPNext installed and `allow_tests` enabled:

```sh
bench --site <synthetic-site> run-tests --app erpnext --module erpnext.erpnext_integrations.test_ledger_authority
bench --site <synthetic-site> run-tests --app erpnext --module erpnext.erpnext_integrations.test_ledger_shadow
```

The native test fixture creates a synthetic USD company and accounts. Its voucher-comparison test submits one synthetic native journal as the comparison fixture; the adapter itself does not submit it. Accounting-table fingerprints prove no further accounting/stock writes around shadow calls. Database tests must actually pass before their coverage is claimed. These tests are not statutory or production acceptance evidence.
