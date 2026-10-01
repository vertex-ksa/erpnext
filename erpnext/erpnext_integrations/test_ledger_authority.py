# Copyright (c) 2026, Technominds and Contributors
# License: GNU General Public License v3. See license.txt

import copy
import unittest
from decimal import Decimal

from erpnext.erpnext_integrations.ledger_authority import (
	LedgerBoundaryError,
	authority_for,
	compare_lines,
	money,
	proposal,
)


def fixtures():
	configuration = {
		"tenant_id": "synthetic-tenant",
		"environment": "test",
		"authorities": [
			{
				"legal_entity_id": "entity-one",
				"provider": "ERPNEXT",
				"posting_mode": "SHADOW",
				"provider_company_id": "Synthetic Company",
				"base_currency": "SAR",
				"currency_precision": 2,
				"effective_from": "2026-01-01",
				"policy_version": "synthetic-policy-one",
				"accounts": {"receivable": "Receivable - SC", "revenue": "Revenue - SC"},
			}
		],
	}
	batch = {
		"contract_version": "C11/ledger-shadow-v1",
		"tenant_id": "synthetic-tenant",
		"legal_entity_id": "entity-one",
		"environment": "test",
		"source_app": "INVOICENINJA",
		"source_id": "invoice-415",
		"source_version": 1,
		"source_hash": "a" * 64,
		"posting_purpose": "INVOICE_ISSUED",
		"posting_date": "2026-10-01",
		"currency": "SAR",
		"debit_total": "1000.00",
		"credit_total": "1000.00",
		"lines": [
			{"account_key": "receivable", "debit": "1000.00", "credit": "0.00"},
			{"account_key": "revenue", "debit": "0.00", "credit": "1000.00"},
		],
	}
	return configuration, batch


class TestLedgerAuthority(unittest.TestCase):
	def test_malformed_nested_values_fail_as_boundary_errors(self):
		configuration, batch = fixtures()
		for invalid in [None, True, [], {}, 1.5]:
			with self.subTest(invalid=invalid):
				with self.assertRaises(LedgerBoundaryError):
					proposal(configuration, batch | {"posting_purpose": invalid})
				with self.assertRaises(LedgerBoundaryError):
					proposal(configuration | {"environment": invalid}, batch)
				for key in ("provider", "posting_mode", "currency_precision"):
					changed = copy.deepcopy(configuration)
					changed["authorities"][0][key] = invalid
					with self.assertRaises(LedgerBoundaryError):
						proposal(changed, batch)

	def test_one_thousand_synthetic_balanced_amounts_and_exact_mismatches(self):
		config, batch = fixtures()
		for cents in range(1, 1001):
			with self.subTest(cents=cents):
				amount = f"{Decimal(cents) / 100:.2f}"
				changed = batch | {
					"debit_total": amount,
					"credit_total": amount,
					"lines": [
						{"account_key": "receivable", "debit": amount, "credit": "0"},
						{"account_key": "revenue", "debit": "0", "credit": amount},
					],
				}
				expected = proposal(config, changed)
				self.assertEqual(expected["debit_total"], amount)
				self.assertEqual(compare_lines(expected["lines"], expected["lines"]), "MATCH")
				incorrect = copy.deepcopy(expected["lines"])
				incorrect[0]["debit"] = f"{Decimal(amount) + Decimal('0.01'):.2f}"
				self.assertEqual(compare_lines(expected["lines"], incorrect), "MISMATCH")

	def test_equivalent_decimal_encodings_have_the_same_canonical_hash(self):
		config, batch = fixtures()
		first = proposal(config, batch)
		batch["lines"][0]["debit"] = "1000"
		batch["lines"][0]["credit"] = "0"
		self.assertEqual(first["payload_hash"], proposal(config, batch)["payload_hash"])

	def test_shadow_is_source_linked_stable_and_never_posted(self):
		config, batch = fixtures()
		first = proposal(config, batch)
		self.assertEqual(first, proposal(config, copy.deepcopy(batch)))
		self.assertFalse(first["posted"])
		self.assertEqual(first["posting_mode"], "SHADOW")
		self.assertEqual(first["source"]["source_hash"], "a" * 64)

	def test_changed_source_payload_retains_business_key_but_changes_hash(self):
		config, batch = fixtures()
		first = proposal(config, batch)
		batch["source_hash"] = "b" * 64
		second = proposal(config, batch)
		self.assertEqual(first["business_key"], second["business_key"])
		self.assertNotEqual(first["payload_hash"], second["payload_hash"])

	def test_identity_separates_entity_environment_version_and_purpose(self):
		config, batch = fixtures()
		initial = proposal(config, batch)["business_key"]
		for field, value in [("source_version", 2), ("posting_purpose", "CUSTOMER_RECEIPT")]:
			with self.subTest(field=field):
				changed = batch | {field: value}
				self.assertNotEqual(initial, proposal(config, changed)["business_key"])

	def test_rejects_wrong_scope_unsupported_facts_and_unbalanced_batches(self):
		config, batch = fixtures()
		for field, value in [
			("tenant_id", "other"),
			("legal_entity_id", "other"),
			("environment", "production"),
			("source_app", "TWENTY"),
			("posting_purpose", "PAYMENT_NOTIFICATION"),
			("currency", "USD"),
			("posting_date", "2025-12-31"),
			("source_version", True),
			("source_hash", "invalid"),
			("debit_total", "999.00"),
			("posting_date", "2026-02-30"),
		]:
			with self.subTest(field=field, value=value), self.assertRaises(LedgerBoundaryError):
				proposal(config, batch | {field: value})

	def test_standalone_disabled_live_and_other_providers_cannot_use_adapter(self):
		for provider, mode in [
			("NONE", "DISABLED"),
			("ERPNEXT", "DISABLED"),
			("ERPNEXT", "LIVE"),
			("AKAUNTING", "SHADOW"),
			("EXTERNAL", "SHADOW"),
			("NONE", "SHADOW"),
		]:
			config, batch = fixtures()
			config["authorities"][0].update(provider=provider, posting_mode=mode)
			with self.subTest(provider=provider, mode=mode), self.assertRaises(LedgerBoundaryError):
				proposal(config, batch)

	def test_duplicate_entity_and_company_authorities_are_rejected(self):
		config, batch = fixtures()
		for entity in ["entity-one", "entity-two"]:
			with self.subTest(entity=entity), self.assertRaises(LedgerBoundaryError):
				authority_for(
					config
					| {
						"authorities": config["authorities"]
						+ [config["authorities"][0] | {"legal_entity_id": entity}]
					},
					"entity-one",
					batch["posting_date"],
				)

	def test_money_rejects_implicit_rounding_floats_and_capacity(self):
		for value in [1.0, True, "NaN", "1e3", "-1", "01", "1.001", "1000000000001", "9" * 100]:
			with self.subTest(value=value), self.assertRaises(LedgerBoundaryError):
				money(value, 2)

	def test_account_mappings_and_one_positive_side_are_required(self):
		config, batch = fixtures()
		for line in [
			{"account_key": "missing", "debit": "1", "credit": "0"},
			{"account_key": "receivable", "debit": "1", "credit": "1"},
			{"account_key": "receivable", "debit": "0", "credit": "0"},
		]:
			with self.subTest(line=line), self.assertRaises(LedgerBoundaryError):
				proposal(config, batch | {"lines": [line, batch["lines"][1]]})

	def test_reversal_requires_original_reference_and_new_identity(self):
		config, batch = fixtures()
		with self.assertRaises(LedgerBoundaryError):
			proposal(config, batch | {"posting_purpose": "REVERSAL"})
		reversal = proposal(
			config, batch | {"posting_purpose": "REVERSAL", "reversal_of": "native-voucher-one"}
		)
		self.assertNotEqual(proposal(config, batch)["business_key"], reversal["business_key"])
		self.assertEqual(reversal["source"]["reversal_of"], "native-voucher-one")

	def test_comparison_aggregates_rows_without_hiding_differences(self):
		expected = [{"account": "AR", "debit": "1000.00", "credit": "0"}]
		self.assertEqual(
			compare_lines(
				expected,
				[
					{"account": "AR", "debit": "400", "credit": "0"},
					{"account": "AR", "debit": "600", "credit": "0"},
				],
			),
			"MATCH",
		)
		self.assertEqual(
			compare_lines(expected, [{"account": "AR", "debit": "999.99", "credit": "0"}]), "MISMATCH"
		)
