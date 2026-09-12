"""Behavior checks using temporary databases; never touches real expenses."""

import asyncio
import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import flet as ft
import msgpack
from flet.controls.base_control import BaseControl
from flet.messaging.protocol import configure_encode_object_for_msgpack
from flet.messaging.session import Session

from main import (
    CATEGORIES, ExpenseRepository, ExpenseTracker, category_totals, format_inr,
    main, parse_amount, parse_date,
)


class MoneyTests(unittest.TestCase):
    def test_exact_paise_and_indian_grouping(self):
        self.assertEqual(parse_amount(" 123456.78 "), 12345678)
        self.assertEqual(parse_amount("0.10") + parse_amount("0.20"), 30)
        for paise, expected in [(0, "₹0.00"), (1, "₹0.01"), (12345, "₹123.45"),
                                (12345678, "₹1,23,456.78"),
                                (99999999999, "₹99,99,99,999.99")]:
            with self.subTest(paise=paise):
                self.assertEqual(format_inr(paise), expected)

    def test_invalid_amounts(self):
        for value in ["", "0", "-1", "1.001", "NaN", "Infinity", "1e3", "1,000",
                      "1000000000", "1_000", "abc", "9" * 1000]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_amount(value)

    def test_calendar_validation(self):
        self.assertEqual(parse_date("2024-02-29"), date(2024, 2, 29))
        for value in ["2025-02-29", "2026-13-01", "20260912", "2026-9-12", ""]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_date(value)


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "test.db"
        self.repo = ExpenseRepository(self.path)

    def test_persistence_sorting_and_delete(self):
        self.repo.add(10, "AI", date(2026, 9, 12), "First")
        self.repo.add(20, "Food", date(2025, 12, 31), "Older")
        self.repo.add(30, "Cloud", date(2026, 9, 12), "Latest")
        reopened = ExpenseRepository(self.path)
        rows = reopened.list_all()
        self.assertEqual([row.description for row in rows], ["Latest", "First", "Older"])
        self.assertEqual(sum(row.amount_paise for row in rows), 60)
        reopened.delete(rows[0].id)
        self.assertEqual(sum(row.amount_paise for row in self.repo.list_all()), 30)
        self.assertEqual(len(self.repo.list_all()), 2)

    def test_all_categories_and_parameterized_description(self):
        description = "Lunch'); DROP TABLE expenses; --"
        for category in CATEGORIES:
            self.repo.add(100, category, date.today(), description)
        self.assertEqual(len(self.repo.list_all()), 7)
        self.assertEqual(self.repo.list_all()[0].description, description)
        with self.assertRaises(ValueError):
            self.repo.add(100, "Other", date.today(), "")
        with self.assertRaises(ValueError):
            self.repo.add(1.5, "General", date.today(), "")
        with self.assertRaises(ValueError):
            self.repo.add(100, "General", date.today(), "x" * 201)

    def test_ui_add_validation_delete_and_summary(self):
        # Construct real Flet controls with a mocked transport; no GUI required.
        page = Mock(spec=ft.Page)
        app = ExpenseTracker(page, self.repo)
        app.build()
        self.assertEqual([option.key for option in app.category.options], list(CATEGORIES))
        self.assertEqual(app.total.value, "₹0.00")
        app.amount.value = "invalid"
        app.add_expense(None)
        self.assertIsNotNone(app.amount.error)
        self.assertEqual(self.repo.list_all(), [])
        app.amount.value = "123456.78"
        app.spent_on.value = "2026-09-12"
        app.description.value = "Test expense"
        app.add_expense(None)
        self.assertEqual(app.total.value, "₹1,23,456.78")
        self.assertEqual(app.count.value, "1 expense recorded")
        self.assertEqual(app.amount.value, "")
        self.assertIsNone(app.amount.error)
        expense = self.repo.list_all()[0]
        app.delete_expense(expense)
        self.assertEqual(app.total.value, "₹0.00")
        self.assertEqual(self.repo.list_all(), [])

    def test_failed_save_preserves_form(self):
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        app.amount.value = "50.00"
        app.description.value = "Keep this"
        app.repository.add = Mock(side_effect=sqlite3.OperationalError("locked"))
        with self.assertLogs(level="ERROR"):
            app.add_expense(None)
        self.assertEqual(app.amount.value, "50.00")
        self.assertEqual(app.description.value, "Keep this")
        self.assertIn("Could not save", app.status.value)
        self.assertEqual(self.repo.list_all(), [])

    def test_budget_survives_restart_and_updates_one_record(self):
        self.assertIsNone(self.repo.get_budget())
        self.repo.set_budget(12345678)
        reopened = ExpenseRepository(self.path)
        self.assertEqual(reopened.get_budget(), 12345678)
        reopened.set_budget(250000)
        self.assertEqual(self.repo.get_budget(), 250000)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM budget").fetchone()[0], 1)
        reopened.set_budget(0)
        self.assertEqual(ExpenseRepository(self.path).get_budget(), 0)

    def test_migration_preserves_existing_expenses(self):
        self.repo.add(125050, "Food", date(2026, 9, 12), "Existing expense")
        original = self.repo.list_all()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE budget")  # Original app's schema.
        migrated = ExpenseRepository(self.path)
        self.assertEqual(migrated.list_all(), original)
        self.assertIsNone(migrated.get_budget())
        migrated.set_budget(500000)
        self.assertEqual(ExpenseRepository(self.path).get_budget(), 500000)
        self.assertEqual(migrated.list_all(), original)

    def test_budget_validation_preserves_saved_value(self):
        self.repo.set_budget(5000)
        for amount in [-1, 1.5, True, 100000000000]:
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    self.repo.set_budget(amount)
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        for value in ["", "-10", "1.001", "NaN", "1e3", "1000000000"]:
            with self.subTest(value=value):
                app.budget_amount.value = value
                app.save_budget(None)
                self.assertIsNotNone(app.budget_amount.error)
                self.assertEqual(self.repo.get_budget(), 5000)
        app.budget_amount.value = "0"
        app.save_budget(None)
        self.assertIsNone(app.budget_amount.error)
        self.assertEqual(self.repo.get_budget(), 0)
        self.assertEqual(app.progress.value, 0)

    def test_budget_dashboard_tracks_expenses_and_excess_spending(self):
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        app.build()
        self.assertEqual(app.tabs.length, 2)
        self.assertEqual(app.tabs.selected_index, 0)
        self.assertEqual(app.progress.value, 0)
        self.assertIn("No budget set", app.utilization.value)
        app.budget_amount.value = "100.00"
        app.save_budget(None)
        self.assertEqual(app.budget_total.value, "₹100.00")
        self.assertEqual(app.remaining.value, "₹100.00")
        app.amount.value = "25.00"
        app.add_expense(None)
        self.assertEqual(app.budget_spent.value, "₹25.00")
        self.assertEqual(app.remaining.value, "₹75.00")
        self.assertEqual(app.progress.value, 0.25)
        self.assertIn("25.0%", app.utilization.value)
        # Preserve an unsaved budget edit when another tab changes expenses.
        app.budget_amount.value = "200.00"
        app.amount.value = "100.00"
        app.add_expense(None)
        self.assertEqual(app.budget_amount.value, "200.00")
        self.assertEqual(app.remaining.value, "−₹25.00")
        self.assertEqual(app.progress.value, 1)
        self.assertIn("125.0%", app.utilization.value)
        self.assertIn("Over budget by ₹25.00", app.utilization.value)
        app.delete_expense(self.repo.list_all()[0])
        self.assertEqual(app.remaining.value, "₹75.00")
        self.assertEqual(app.progress.value, 0.25)
        app.save_budget(None)
        self.assertEqual(app.remaining.value, "₹175.00")
        self.assertEqual(app.progress.value, 0.125)
        restarted = ExpenseTracker(Mock(spec=ft.Page), ExpenseRepository(self.path))
        restarted.build()
        self.assertEqual(restarted.budget_amount.value, "200.00")
        self.assertEqual(restarted.remaining.value, "₹175.00")

    def test_zero_and_exactly_exhausted_budget(self):
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        app.update_budget_summary(1000, 1000)
        self.assertEqual(app.remaining.value, "₹0.00")
        self.assertEqual(app.progress.value, 1)
        self.assertIn("Budget fully utilized", app.utilization.value)
        app.update_budget_summary(0, 1000)
        self.assertEqual(app.remaining.value, "−₹10.00")
        self.assertEqual(app.progress.value, 1)
        self.assertIn("Over budget by ₹10.00", app.utilization.value)
        app.update_budget_summary(0, 0)
        self.assertEqual(app.progress.value, 0)
        self.assertEqual(app.remaining.value, "₹0.00")

    def test_failed_budget_save_preserves_form_and_database(self):
        self.repo.set_budget(10000)
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        app.build()
        app.budget_amount.value = "200.00"
        self.repo.set_budget = Mock(side_effect=sqlite3.OperationalError("locked"))
        with self.assertLogs(level="ERROR"):
            app.save_budget(None)
        self.assertEqual(app.budget_amount.value, "200.00")
        self.assertEqual(app.budget_total.value, "₹100.00")
        self.assertEqual(self.repo.get_budget(), 10000)
        self.assertIn("Could not save the budget", app.status.value)

    def test_category_breakdown_groups_all_dates_and_loads_on_restart(self):
        self.repo.add(10, "AI", date(2025, 1, 1), "Older")
        self.repo.add(20, "AI", date(2026, 9, 12), "Newer")
        self.repo.add(70, "Cloud", date(2026, 9, 12), "Cloud")
        totals = category_totals(self.repo.list_all())
        self.assertEqual(list(totals), list(CATEGORIES))
        self.assertEqual(totals["AI"], 30)
        self.assertEqual(totals["Cloud"], 70)
        self.assertEqual(totals["Food"], 0)
        app = ExpenseTracker(Mock(spec=ft.Page), ExpenseRepository(self.path))
        app.build()
        self.assertIsNone(self.repo.get_budget())
        self.assertTrue(app.spending_chart.visible)
        self.assertFalse(app.chart_empty.visible)
        self.assertEqual(app.category_amounts["AI"].value, "₹0.30")
        self.assertEqual(app.category_percentages["AI"].value, "30.0%")
        self.assertEqual(app.category_percentages["Cloud"].value, "70.0%")
        self.assertEqual(app.category_percentages["Food"].value, "0.0%")
        app.budget_amount.value = "500.00"
        app.save_budget(None)
        self.assertEqual(app.category_percentages["AI"].value, "30.0%")

    def test_tiny_category_share_still_has_exact_amount(self):
        self.repo.add(1, "Repairs", date.today(), "Tiny expense")
        self.repo.add(10000, "General", date.today(), "Larger expense")
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        app.build()
        self.assertEqual(app.category_amounts["Repairs"].value, "₹0.01")
        self.assertEqual(app.category_percentages["Repairs"].value, "<0.1%")
        self.assertEqual(len(app.spending_chart.sections), 2)


class FletStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_mounted_page_startup_and_budget_updates(self):
        # Use the real mount/validation/serialization path. Mock only the socket.
        # Constructing controls with Mock(Page) alone misses deferred validation.
        connection = Mock()
        connection.loop = asyncio.get_running_loop()
        encode = configure_encode_object_for_msgpack(BaseControl)
        connection.send_message.side_effect = lambda message: msgpack.packb(
            [message.action, message.body], default=encode,
        )
        session = Session(connection)
        with tempfile.TemporaryDirectory() as directory:
            repository = ExpenseRepository(Path(directory) / "test.db")
            app = ExpenseTracker(session.page, repository)
            with patch("main.ExpenseRepository", return_value=repository), \
                    patch("main.ExpenseTracker", return_value=app):
                main(session.page)
            # Flet validates the entire control tree during client registration.
            payload = msgpack.packb(session.get_page_patch(), default=encode)
            self.assertTrue(payload)
            self.assertIn("No budget set", app.progress.semantics_label)
            self.assertFalse(app.spending_chart.visible)
            self.assertTrue(app.chart_empty.visible)

            app.budget_amount.value = "100.00"
            app.save_budget(None)
            app.amount.value = "125.00"
            app.add_expense(None)
            self.assertEqual(app.remaining.value, "−₹25.00")
            self.assertIn("125.0%", app.progress.semantics_label)
            self.assertTrue(app.spending_chart.visible)
            self.assertEqual(app.category_percentages["General"].value, "100.0%")

            app.budget_amount.value = "0"
            app.save_budget(None)
            self.assertIn("No funds available", app.progress.semantics_label)
            app.delete_expense(repository.list_all()[0])
            self.assertEqual(app.progress.value, 0)
            self.assertEqual(app.spending_chart.sections, [])
            self.assertTrue(app.chart_empty.visible)
            self.assertEqual(app.category_amounts["General"].value, "₹0.00")

            # Mounted charts must serialize correctly as categories appear,
            # aggregate, and change after deletion.
            for category, amount in [("Food", "75"), ("Cloud", "25"), ("Food", "25")]:
                app.category.value = category
                app.amount.value = amount
                app.add_expense(None)
            self.assertEqual(
                {section.key: section.value for section in app.spending_chart.sections},
                {"Cloud": 2500, "Food": 10000},
            )
            self.assertEqual(app.category_amounts["Food"].value, "₹100.00")
            self.assertEqual(app.category_percentages["Food"].value, "80.0%")
            app.delete_expense(repository.list_all()[0])
            self.assertEqual(app.category_amounts["Food"].value, "₹75.00")
            self.assertEqual(app.category_percentages["Food"].value, "75.0%")
            self.assertEqual(app.category_percentages["Cloud"].value, "25.0%")
            self.assertTrue(connection.send_message.called)


class CsvExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.repo = ExpenseRepository(self.folder / "test.db")
        self.app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        self.app.build()
        self.destination = self.folder / "my expenses.csv"

    def read_export(self):
        with self.destination.open(encoding="utf-8-sig", newline="") as exported:
            return list(csv.reader(exported))

    async def test_exports_fresh_database_with_exact_values_and_quoting(self):
        # Add after the UI was built so export cannot rely on cached history.
        description = 'Lunch, "with friends"\nPaid ₹250 • भोजन'
        self.repo.add(125050, "Food", date(2025, 1, 1), description)
        self.repo.add(1, "AI", date(2026, 9, 12), "")
        original = self.repo.list_all()
        with patch.object(self.app.file_picker, "save_file", new_callable=AsyncMock,
                          return_value=str(self.destination)) as picker:
            await self.app.export_csv(None)
        picker.assert_awaited_once()
        self.assertEqual(picker.call_args.kwargs["allowed_extensions"], ["csv"])
        self.assertEqual(picker.call_args.kwargs["file_type"], ft.FilePickerFileType.CUSTOM)
        self.assertEqual(self.read_export(), [
            ["ID", "Amount", "Category", "Date", "Description"],
            [str(original[0].id), "0.01", "AI", "2026-09-12", ""],
            [str(original[1].id), "1250.50", "Food", "2025-01-01", description],
        ])
        self.assertEqual(self.repo.list_all(), original)
        self.assertIn("Exported 2 expenses", self.app.status.value)
        self.assertFalse(self.app.export_button.disabled)

    async def test_empty_database_exports_headers(self):
        with patch.object(self.app.file_picker, "save_file", new_callable=AsyncMock,
                          return_value=str(self.destination)):
            await self.app.export_csv(None)
        self.assertEqual(self.read_export(), [
            ["ID", "Amount", "Category", "Date", "Description"],
        ])
        self.assertIn("Exported 0 expenses", self.app.status.value)

    async def test_cancel_does_not_query_or_write(self):
        with patch.object(self.app.file_picker, "save_file", new_callable=AsyncMock,
                          return_value=None), patch.object(self.repo, "list_all") as query:
            await self.app.export_csv(None)
        query.assert_not_called()
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.app.status.value, "Export cancelled.")
        self.assertFalse(self.app.export_button.disabled)

    async def test_failed_write_reports_error_and_reenables_button(self):
        with patch.object(self.app.file_picker, "save_file", new_callable=AsyncMock,
                          return_value=str(self.destination)), \
                patch.object(Path, "open", side_effect=PermissionError("Read only")), \
                self.assertLogs(level="ERROR"):
            await self.app.export_csv(None)
        self.assertIn("Could not export", self.app.status.value)
        self.assertFalse(self.app.export_button.disabled)
        self.assertFalse(self.destination.exists())

    async def test_query_failure_preserves_existing_destination(self):
        self.destination.write_text("Existing export")
        with patch.object(self.app.file_picker, "save_file", new_callable=AsyncMock,
                          return_value=str(self.destination)), \
                patch.object(self.repo, "list_all", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs(level="ERROR"):
            await self.app.export_csv(None)
        self.assertEqual(self.destination.read_text(), "Existing export")
        self.assertIn("Could not export", self.app.status.value)
        self.assertFalse(self.app.export_button.disabled)


if __name__ == "__main__":
    unittest.main()
