"""Behavior checks using temporary databases; never touches real expenses."""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import flet as ft

from main import (
    CATEGORIES, ExpenseRepository, ExpenseTracker, format_inr, parse_amount, parse_date,
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


if __name__ == "__main__":
    unittest.main()
