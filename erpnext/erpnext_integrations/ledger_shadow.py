# Copyright (c) 2026, Technominds and Contributors
# License: GNU General Public License v3. See license.txt

import json

import frappe

from erpnext.accounts.services.gl_validator import (
	check_freezing_date,
	validate_accounting_period,
	validate_against_pcv,
	validate_disabled_accounts,
)
from erpnext.accounts.utils import get_fiscal_year
from erpnext.erpnext_integrations.ledger_authority import (
	LedgerBoundaryError,
	compare_lines,
	proposal,
)


def _validated_proposal(batch):
	if frappe.session.user == "Guest":
		raise frappe.PermissionError
	try:
		result = proposal(frappe.conf.get("technominds_ledger"), batch)
	except LedgerBoundaryError as error:
		frappe.throw(str(error))
	company = frappe.get_doc("Company", result["company"])
	company.check_permission("read")
	if company.name != result["company"]:
		frappe.throw("The authority must use the canonical native company reference")
	frappe.has_permission("Journal Entry", "create", throw=True)
	if company.default_currency != result["currency"]:
		frappe.throw("The authority currency does not match the native company")
	gl_map = []
	for line in result["lines"]:
		account = frappe.get_doc("Account", line["account"])
		account.check_permission("read")
		if account.name != line["account"]:
			frappe.throw("Account mappings must use canonical native account references")
		if account.company != company.name or account.is_group or account.disabled:
			frappe.throw("The native account is not an active posting account for this company")
		if account.account_currency != result["currency"]:
			frappe.throw("Foreign account currencies are not supported by this shadow adapter")
		gl_map.append(
			frappe._dict(
				line
				| {
					"company": company.name,
					"posting_date": result["posting_date"],
					"voucher_type": "Journal Entry",
				}
			)
		)
	get_fiscal_year(result["posting_date"], company=company.name)
	check_freezing_date(result["posting_date"], company.name)
	validate_accounting_period(gl_map)
	validate_against_pcv(False, result["posting_date"], company.name)
	validate_disabled_accounts(gl_map)
	if result["source"]["reversal_of"]:
		original = frappe.get_doc("Journal Entry", result["source"]["reversal_of"])
		original.check_permission("read")
		if original.company != company.name or original.docstatus != 1:
			frappe.throw("A reversal proposal must reference a submitted journal in the elected company")
	return result


def _remember(result):
	# A unique native name retains the business key across retries; Redis expiry must not reset it.
	name = "tm-ledger-shadow-" + result["business_key"]
	if frappe.db.exists("Integration Request", name):
		stored = json.loads(frappe.get_doc("Integration Request", name).output)
		if stored["payload_hash"] != result["payload_hash"]:
			frappe.throw("This source version was already observed with a different posting payload")
		return name
	frappe.db.savepoint("technominds_shadow_receipt")
	try:
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Technominds Ledger Shadow",
				"status": "Completed",
				"data": frappe.as_json(result["source"]),
				"output": frappe.as_json(result),
			}
		).insert(ignore_permissions=True, set_name=name)
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="technominds_shadow_receipt")
		# Locking read sees the winning receipt under MariaDB's repeatable-read isolation.
		stored_output = frappe.db.sql(
			"select output from `tabIntegration Request` where name=%s for update", name
		)
		if not stored_output:
			frappe.throw("The concurrent shadow receipt could not be reconciled")
		stored = json.loads(stored_output[0][0])
		if stored["payload_hash"] != result["payload_hash"]:
			frappe.throw("Concurrent requests disagree about the posting payload")
	return name


@frappe.whitelist(methods=["POST"])
def preview_batch(batch: dict):
	"""Record a validated shadow proposal; never create or submit a native accounting voucher."""
	result = _validated_proposal(batch)
	result["receipt_id"] = _remember(result)
	return result


@frappe.whitelist(methods=["POST"])
def compare_voucher(batch: dict, voucher_type: str, voucher_id: str):
	"""Compare base-currency account totals only; a match never enables LIVE posting."""
	result = _validated_proposal(batch)
	if voucher_type not in {"Sales Invoice", "Payment Entry", "Journal Entry"}:
		frappe.throw("Unsupported native voucher type")
	voucher = frappe.get_doc(voucher_type, voucher_id)
	voucher.check_permission("read")
	if voucher.company != result["company"] or voucher.docstatus != 1:
		frappe.throw("Comparison requires a submitted voucher in the elected company")
	if str(voucher.posting_date) != result["posting_date"]:
		frappe.throw("The native voucher has a different posting date")
	frappe.has_permission("GL Entry", "read", throw=True)
	filters = {
		"company": result["company"],
		"voucher_type": voucher_type,
		"voucher_no": voucher_id,
		"is_cancelled": 0,
	}
	actual = frappe.get_list(
		"GL Entry",
		filters=filters,
		fields=["account", "debit", "credit"],
		limit_page_length=501,
	)
	if len(actual) > 500:
		frappe.throw("Native voucher exceeds the complete-comparison limit")
	if len(actual) != frappe.db.count("GL Entry", filters):
		frappe.throw("The current permissions do not expose the complete native voucher ledger")
	result.update(
		comparison=compare_lines(result["lines"], actual) if actual else "MISSING",
		comparison_scope="BASE_CURRENCY_ACCOUNT_TOTALS",
		live_eligible=False,
		voucher_type=voucher_type,
		voucher_id=voucher_id,
	)
	return result
