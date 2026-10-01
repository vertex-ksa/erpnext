# Copyright (c) 2026, Technominds and Contributors
# License: GNU General Public License v3. See license.txt

import hashlib
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

PROVIDERS = frozenset({"NONE", "ERPNEXT", "AKAUNTING", "EXTERNAL"})
POSTING_MODES = frozenset({"DISABLED", "SHADOW", "LIVE"})
PURPOSES = frozenset({"INVOICE_ISSUED", "CUSTOMER_RECEIPT", "REVERSAL"})


class LedgerBoundaryError(ValueError):
	pass


def digest(value):
	return hashlib.sha256(
		json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
	).hexdigest()


def identifier(value):
	if not isinstance(value, str) or not value.strip() or len(value) > 140:
		raise LedgerBoundaryError("A bounded, nonempty reference is required")
	return value


def iso_date(value):
	if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
		raise LedgerBoundaryError("Use an ISO calendar date")
	try:
		return date.fromisoformat(value)
	except ValueError as error:
		raise LedgerBoundaryError("Invalid calendar date") from error


def authority_for(configuration, legal_entity_id, posting_date):
	if not isinstance(configuration, dict):
		raise LedgerBoundaryError("Ledger integration is disabled until configured")
	identifier(configuration.get("tenant_id"))
	if not isinstance(configuration.get("environment"), str) or configuration["environment"] not in {
		"test",
		"sandbox",
		"production",
	}:
		raise LedgerBoundaryError("A trusted deployment environment is required")
	entries = configuration.get("authorities")
	if not isinstance(entries, list) or len(entries) > 1000:
		raise LedgerBoundaryError("Explicit entity authority mappings are required")
	entities, companies = set(), set()
	selected = None
	for entry in entries:
		if not isinstance(entry, dict):
			raise LedgerBoundaryError("Invalid authority mapping")
		entity = identifier(entry.get("legal_entity_id"))
		if entity in entities:
			raise LedgerBoundaryError("Only one ledger authority is allowed per legal entity")
		entities.add(entity)
		provider, mode = entry.get("provider"), entry.get("posting_mode")
		if (
			not isinstance(provider, str)
			or not isinstance(mode, str)
			or provider not in PROVIDERS
			or mode not in POSTING_MODES
		):
			raise LedgerBoundaryError("Invalid provider or posting mode")
		if provider == "NONE" and mode != "DISABLED":
			raise LedgerBoundaryError("Standalone entities cannot enable accounting posting")
		if provider != "NONE":
			company = identifier(entry.get("provider_company_id"))
			company_key = (provider, company)
			if company_key in companies:
				raise LedgerBoundaryError("A provider company cannot represent two legal entities")
			companies.add(company_key)
		if entity == legal_entity_id:
			selected = entry
	if selected is None or selected["posting_mode"] == "DISABLED":
		raise LedgerBoundaryError("Accounting integration is disabled for this legal entity")
	if selected["posting_mode"] == "LIVE":
		raise LedgerBoundaryError("LIVE posting is unavailable; shadow reconciliation is required")
	if selected["provider"] != "ERPNEXT":
		raise LedgerBoundaryError("This adapter only handles the elected ERPNext ledger")
	if iso_date(posting_date) < iso_date(selected.get("effective_from")):
		raise LedgerBoundaryError("The ledger authority is not effective on the posting date")
	if not re.fullmatch(r"[A-Z]{3}", str(selected.get("base_currency", ""))):
		raise LedgerBoundaryError("An explicit base currency is required")
	precision = selected.get("currency_precision")
	if not isinstance(precision, int) or isinstance(precision, bool) or not 0 <= precision <= 6:
		raise LedgerBoundaryError("An approved currency precision from zero to six is required")
	identifier(selected.get("policy_version"))
	return selected


def money(value, precision):
	if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r"(0|[1-9]\d*)(\.\d+)?", value):
		raise LedgerBoundaryError("Money must be a nonnegative plain decimal string")
	try:
		amount = Decimal(value)
		quantum = Decimal(1).scaleb(-precision)
		if amount > Decimal("1000000000000") or amount.quantize(quantum) != amount:
			raise LedgerBoundaryError("Money exceeds the supported exact precision or capacity")
		return amount
	except InvalidOperation as error:
		raise LedgerBoundaryError("Invalid exact money") from error


def proposal(configuration, batch):
	if not isinstance(batch, dict):
		raise LedgerBoundaryError("A posting batch is required")
	allowed = {
		"contract_version",
		"tenant_id",
		"legal_entity_id",
		"environment",
		"source_app",
		"source_id",
		"source_version",
		"source_hash",
		"posting_purpose",
		"posting_date",
		"currency",
		"debit_total",
		"credit_total",
		"lines",
		"reversal_of",
	}
	if set(batch) - allowed:
		raise LedgerBoundaryError("Unknown posting batch fields")
	if batch.get("contract_version") != "C11/ledger-shadow-v1":
		raise LedgerBoundaryError("Unsupported shadow posting contract version")
	entity = identifier(batch.get("legal_entity_id"))
	authority = authority_for(configuration, entity, batch.get("posting_date"))
	if (
		batch.get("tenant_id") != configuration["tenant_id"]
		or batch.get("environment") != configuration["environment"]
	):
		raise LedgerBoundaryError("The batch does not belong to this deployment")
	if (
		batch.get("source_app") != "INVOICENINJA"
		or not isinstance(batch.get("posting_purpose"), str)
		or batch["posting_purpose"] not in PURPOSES
	):
		raise LedgerBoundaryError(
			"Unsupported source fact; CRM, notifications and stock projections cannot post"
		)
	identifier(batch.get("source_id"))
	if (
		not isinstance(batch.get("source_version"), int)
		or isinstance(batch["source_version"], bool)
		or batch["source_version"] < 1
	):
		raise LedgerBoundaryError("A positive source version is required")
	if not isinstance(batch.get("source_hash"), str) or not re.fullmatch(
		r"[a-f0-9]{64}", batch["source_hash"]
	):
		raise LedgerBoundaryError("A canonical source hash is required")
	if batch.get("currency") != authority["base_currency"]:
		raise LedgerBoundaryError("The first adapter supports the elected company's base currency only")
	if batch["posting_purpose"] == "REVERSAL":
		identifier(batch.get("reversal_of"))
	elif batch.get("reversal_of") is not None:
		raise LedgerBoundaryError("Only a reversal may reference a previous posting")
	lines = batch.get("lines")
	if not isinstance(lines, list) or not 2 <= len(lines) <= 500:
		raise LedgerBoundaryError("A batch must contain between two and 500 lines")
	accounts = authority.get("accounts")
	if not isinstance(accounts, dict):
		raise LedgerBoundaryError("Explicit native account mappings are required")
	precision = authority["currency_precision"]
	debits = credits = Decimal(0)
	native_lines = []
	for line in lines:
		if not isinstance(line, dict) or set(line) - {"account_key", "debit", "credit"}:
			raise LedgerBoundaryError("Unsupported posting line")
		key = identifier(line.get("account_key"))
		account = identifier(accounts.get(key))
		debit, credit = money(line.get("debit"), precision), money(line.get("credit"), precision)
		if (debit > 0) == (credit > 0):
			raise LedgerBoundaryError("Each line must have exactly one positive side")
		debits += debit
		credits += credit
		native_lines.append(
			{"account": account, "debit": f"{debit:.{precision}f}", "credit": f"{credit:.{precision}f}"}
		)
	if (
		debits != credits
		or debits != money(batch.get("debit_total"), precision)
		or credits != money(batch.get("credit_total"), precision)
	):
		raise LedgerBoundaryError("The posting batch and its control totals must balance exactly")
	identity = {
		key: batch[key]
		for key in (
			"tenant_id",
			"legal_entity_id",
			"environment",
			"source_app",
			"source_id",
			"source_version",
			"posting_purpose",
		)
	}
	result = {
		"contract_version": "C11/ledger-shadow-v1",
		"business_key": digest(identity),
		"provider": "ERPNEXT",
		"posting_mode": "SHADOW",
		"posted": False,
		"live_eligible": False,
		"company": authority["provider_company_id"],
		"currency": batch["currency"],
		"posting_date": batch["posting_date"],
		"policy_version": authority["policy_version"],
		"source": identity | {"source_hash": batch["source_hash"], "reversal_of": batch.get("reversal_of")},
		"debit_total": f"{debits:.{precision}f}",
		"credit_total": f"{credits:.{precision}f}",
		"lines": native_lines,
	}
	result["payload_hash"] = digest(result)
	return result


def compare_lines(expected, actual):
	def totals(lines):
		result = {}
		for line in lines:
			pair = result.setdefault(line["account"], [Decimal(0), Decimal(0)])
			pair[0] += Decimal(str(line["debit"]))
			pair[1] += Decimal(str(line["credit"]))
		return result

	return "MATCH" if totals(expected) == totals(actual) else "MISMATCH"
