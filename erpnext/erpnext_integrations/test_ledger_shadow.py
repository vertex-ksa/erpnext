# Copyright (c) 2026, Technominds and Contributors
# License: GNU General Public License v3. See license.txt

import copy
import hashlib
import json

import frappe

from erpnext.accounts.doctype.journal_entry.test_journal_entry import make_journal_entry
from erpnext.erpnext_integrations.ledger_shadow import compare_voucher, preview_batch
from erpnext.erpnext_integrations.test_ledger_authority import fixtures
from erpnext.tests.utils import ERPNextTestSuite


class TestLedgerShadow(ERPNextTestSuite):
	def setUp(self):
		self.previous_user = frappe.session.user
		self.previous_configuration = copy.deepcopy(frappe.conf.get("technominds_ledger"))
		frappe.set_user("Administrator")
		self.company = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "TM Shadow Synthetic Company",
				"abbr": "TMSC",
				"country": "United States",
				"default_currency": "USD",
			}
		).insert()
		if not frappe.db.exists("Fiscal Year", "TM Shadow Synthetic Year"):
			frappe.get_doc(
				{
					"doctype": "Fiscal Year",
					"year": "TM Shadow Synthetic Year",
					"year_start_date": "2026-01-01",
					"year_end_date": "2026-12-31",
					"companies": [{"company": self.company.name}],
				}
			).insert()
		self.debit_account = frappe.get_all(
			"Account",
			filters={"company": self.company.name, "is_group": 0, "account_type": "Cash"},
			pluck="name",
		)[0]
		self.credit_account = frappe.get_all(
			"Account",
			filters={"company": self.company.name, "is_group": 0, "root_type": "Income"},
			pluck="name",
		)[0]
		self.configuration, self.batch = fixtures()
		self.configuration["authorities"][0].update(
			provider_company_id=self.company.name,
			base_currency="USD",
			accounts={"receivable": self.debit_account, "revenue": self.credit_account},
		)
		self.batch["currency"] = "USD"
		frappe.conf["technominds_ledger"] = self.configuration

	def tearDown(self):
		frappe.conf["technominds_ledger"] = self.previous_configuration
		frappe.set_user(self.previous_user)
		super().tearDown()

	def accounting_fingerprints(self):
		return {
			doctype: hashlib.sha256(
				json.dumps(
					frappe.get_all(doctype, fields=["*"], order_by="name"), sort_keys=True, default=str
				).encode()
			).hexdigest()
			for doctype in (
				"GL Entry",
				"Stock Ledger Entry",
				"Journal Entry",
				"Sales Invoice",
				"Payment Entry",
			)
		}

	def test_native_replay_retains_one_audit_receipt_without_accounting_changes(self):
		before = self.accounting_fingerprints()
		first = preview_batch(self.batch)
		second = preview_batch(copy.deepcopy(self.batch))
		self.assertEqual(first, second)
		self.assertFalse(first["posted"])
		self.assertEqual(frappe.db.count("Integration Request", {"name": first["receipt_id"]}), 1)
		self.assertEqual(before, self.accounting_fingerprints())
		changed = self.batch | {"source_hash": "b" * 64}
		with self.assertRaises(frappe.ValidationError):
			preview_batch(changed)
		self.assertEqual(before, self.accounting_fingerprints())

	def test_guest_cannot_observe_or_store_a_proposal(self):
		with self.set_user("Guest"), self.assertRaises(frappe.PermissionError):
			preview_batch(self.batch)

	def test_disabled_account_revocation_applies_before_warm_receipt_replay(self):
		first = preview_batch(self.batch)
		frappe.get_doc("Account", self.debit_account).db_set("disabled", 1)
		with self.assertRaises(frappe.ValidationError):
			preview_batch(self.batch)
		self.assertEqual(frappe.db.count("Integration Request", {"name": first["receipt_id"]}), 1)

	def test_native_frozen_date_blocks_shadow(self):
		self.company.db_set("accounts_frozen_till_date", self.batch["posting_date"])
		with self.assertRaises(frappe.ValidationError):
			preview_batch(self.batch)

	def test_closed_native_accounting_period_blocks_shadow(self):
		frappe.get_doc(
			{
				"doctype": "Accounting Period",
				"period_name": "TM Shadow Closed Period",
				"company": self.company.name,
				"start_date": "2026-01-01",
				"end_date": self.batch["posting_date"],
				"closed_documents": [{"document_type": "Journal Entry", "closed": 1}],
			}
		).insert()
		with self.assertRaises(frappe.ValidationError):
			preview_batch(self.batch)

	def test_native_role_revocation_blocks_warm_replay(self):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": "ledger-shadow-role@test.invalid",
				"first_name": "Shadow Role",
				"send_welcome_email": 0,
				"roles": [{"role": "Accounts User"}],
			}
		).insert()
		with self.set_user(user.name):
			first = preview_batch(self.batch)
		user.set("roles", [])
		user.save()
		frappe.clear_cache(user=user.name)
		with self.set_user(user.name), self.assertRaises(frappe.PermissionError):
			preview_batch(self.batch)
		self.assertEqual(frappe.db.count("Integration Request", {"name": first["receipt_id"]}), 1)

	def test_account_from_another_native_company_is_rejected(self):
		other = frappe.get_doc(
			{
				"doctype": "Company",
				"company_name": "TM Shadow Other Company",
				"abbr": "TMSO",
				"country": "United States",
				"default_currency": "USD",
			}
		).insert()
		foreign_account = frappe.get_all(
			"Account", filters={"company": other.name, "is_group": 0}, pluck="name"
		)[0]
		self.configuration["authorities"][0]["accounts"]["revenue"] = foreign_account
		with self.assertRaises(frappe.ValidationError):
			preview_batch(self.batch)

	def test_case_aliases_cannot_replace_canonical_native_mappings(self):
		before = self.accounting_fingerprints()
		for field in ("company", "account"):
			configuration = copy.deepcopy(self.configuration)
			if field == "company":
				configuration["authorities"][0]["provider_company_id"] = self.company.name.swapcase()
			else:
				configuration["authorities"][0]["accounts"]["receivable"] = self.debit_account.swapcase()
			frappe.conf["technominds_ledger"] = configuration
			with self.assertRaises((frappe.ValidationError, frappe.DoesNotExistError)):
				preview_batch(self.batch)
		self.assertEqual(before, self.accounting_fingerprints())

	def test_native_voucher_match_and_mismatch_do_not_change_ledger(self):
		voucher = make_journal_entry(
			self.debit_account,
			self.credit_account,
			1000,
			company=self.company.name,
			posting_date=self.batch["posting_date"],
			cost_center=self.company.cost_center,
			submit=True,
		)
		before = self.accounting_fingerprints()
		matched = compare_voucher(self.batch, "Journal Entry", voucher.name)
		self.assertEqual(matched["comparison"], "MATCH")
		self.assertFalse(matched["live_eligible"])
		changed = copy.deepcopy(self.batch)
		changed.update(debit_total="999.00", credit_total="999.00")
		changed["lines"][0]["debit"] = changed["lines"][1]["credit"] = "999.00"
		self.assertEqual(compare_voucher(changed, "Journal Entry", voucher.name)["comparison"], "MISMATCH")
		self.assertEqual(before, self.accounting_fingerprints())

	def test_live_and_absent_configuration_cannot_write_audit_or_accounting(self):
		before = self.accounting_fingerprints()
		for config in [
			None,
			self.configuration
			| {"authorities": [self.configuration["authorities"][0] | {"posting_mode": "LIVE"}]},
		]:
			frappe.conf["technominds_ledger"] = config
			with self.assertRaises(frappe.ValidationError):
				preview_batch(self.batch)
		self.assertEqual(before, self.accounting_fingerprints())
