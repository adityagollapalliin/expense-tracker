"""Behavior checks using temporary databases; never touches real expenses."""

import asyncio
import csv
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
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

    def test_theme_toggle_persists_and_preserves_drafts_and_selected_tab(self):
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        app.build()
        self.assertEqual(app.page.theme_mode, ft.ThemeMode.LIGHT)
        self.assertEqual(app.theme_toggle.icon, ft.Icons.DARK_MODE_OUTLINED)
        app.amount.value = "125.50"
        app.category.value = "AI"
        app.description.value = "Unsaved expense"
        app.budget_amount.value = "5000"
        app.tabs.selected_index = 1
        original_tabs = app.tabs
        app.toggle_theme(None)
        self.assertEqual(app.page.theme_mode, ft.ThemeMode.DARK)
        self.assertEqual(app.theme_toggle.icon, ft.Icons.LIGHT_MODE_OUTLINED)
        self.assertEqual(app.theme_toggle.tooltip, "Switch to light mode")
        self.assertIs(app.tabs, original_tabs)
        self.assertEqual(app.tabs.selected_index, 1)
        self.assertEqual(app.amount.value, "125.50")
        self.assertEqual(app.category.value, "AI")
        self.assertEqual(app.description.value, "Unsaved expense")
        self.assertEqual(app.budget_amount.value, "5000")
        self.assertEqual(self.repo.list_all(), [])
        self.assertIsNone(self.repo.get_budget())

        reopened = ExpenseRepository(self.path)
        self.assertEqual(reopened.get_theme(), "dark")
        restarted = ExpenseTracker(Mock(spec=ft.Page), reopened)
        restarted.build()
        self.assertEqual(restarted.page.theme_mode, ft.ThemeMode.DARK)
        self.assertEqual(restarted.theme_toggle.icon, ft.Icons.LIGHT_MODE_OUTLINED)
        restarted.toggle_theme(None)
        self.assertEqual(restarted.page.theme_mode, ft.ThemeMode.LIGHT)
        self.assertEqual(restarted.theme_toggle.tooltip, "Switch to dark mode")
        self.assertEqual(ExpenseRepository(self.path).get_theme(), "light")

    def test_theme_migration_preserves_expenses_and_budget(self):
        self.repo.add(100, "Food", date.today(), "Existing")
        self.repo.set_budget(5000)
        original = self.repo.list_all()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE preferences")
        migrated = ExpenseRepository(self.path)
        self.assertEqual(migrated.get_theme(), "light")
        migrated.set_theme("dark")
        self.assertEqual(migrated.get_budget(), 5000)
        self.assertEqual(migrated.list_all(), original)
        with self.assertRaises(ValueError):
            migrated.set_theme("invalid")
        self.assertEqual(migrated.get_theme(), "dark")

    def test_theme_storage_errors_keep_app_usable(self):
        app = ExpenseTracker(Mock(spec=ft.Page), self.repo)
        with patch.object(self.repo, "get_theme", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs(level="ERROR"):
            app.build()
        self.assertEqual(app.page.theme_mode, ft.ThemeMode.LIGHT)
        self.assertIn("Could not load your theme", app.status.value)
        with patch.object(self.repo, "set_theme", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs(level="ERROR"):
            app.toggle_theme(None)
        self.assertEqual(app.page.theme_mode, ft.ThemeMode.DARK)
        self.assertEqual(self.repo.get_theme(), "light")
        self.assertIn("could not be saved", app.status.value)

    def test_expense_snackbar_thresholds_and_progress_color(self):
        warning = "Warning: You have used 80% of your budget!"
        alert = "Alert: You have exceeded your budget for this month!"
        cases = [
            (10000, 7999, "Expense added.", ft.Colors.PRIMARY),
            (10000, 8000, warning, "#FFD54F"),
            (10000, 8001, warning, "#FFD54F"),
            (10000, 10000, warning, "#FFD54F"),
            (10000, 10001, alert, "#B3261E"),
            (None, 10001, "Expense added.", ft.Colors.PRIMARY),
            (0, 1, alert, "#B3261E"),
            (3, 2, "Expense added.", ft.Colors.PRIMARY),
            (3, 3, warning, "#FFD54F"),
        ]
        for index, (budget, spent, message, color) in enumerate(cases):
            with self.subTest(budget=budget, spent=spent):
                repo = ExpenseRepository(self.path.with_name(f"alert-{index}.db"))
                if budget is not None:
                    repo.set_budget(budget)
                if spent > 1:
                    repo.add(spent - 1, "AI", date(2025, 1, 1), "Previous spending")
                page = Mock(spec=ft.Page)
                app = ExpenseTracker(page, repo)
                app.build()
                page.show_dialog.assert_not_called()
                app.amount.value = "0.01"
                app.add_expense(None)
                page.show_dialog.assert_called_once_with(app.expense_snackbar)
                self.assertIsInstance(app.expense_snackbar, ft.SnackBar)
                self.assertEqual(app.expense_snackbar.content.value, message)
                self.assertEqual(app.expense_snackbar.bgcolor, color)
                self.assertEqual(app.progress.color,
                                 ft.Colors.ERROR if budget is not None and spent > budget
                                 else ft.Colors.PRIMARY)

    def test_alert_repeats_on_add_but_not_on_other_changes(self):
        self.repo.set_budget(10000)
        page = Mock(spec=ft.Page)
        app = ExpenseTracker(page, self.repo)
        app.build()
        for amount in ["80", "1"]:
            app.amount.value = amount
            app.add_expense(None)
            self.assertEqual(app.expense_snackbar.content.value,
                             "Warning: You have used 80% of your budget!")
        self.assertEqual(page.show_dialog.call_count, 2)
        app.amount.value = "20"
        app.add_expense(None)
        self.assertEqual(app.progress.color, ft.Colors.ERROR)
        self.assertEqual(page.show_dialog.call_count, 3)
        app.delete_expense(self.repo.list_all()[0])
        self.assertEqual(app.progress.color, ft.Colors.PRIMARY)
        app.budget_amount.value = "200"
        app.save_budget(None)
        app.toggle_theme(None)
        app.refresh()
        self.assertEqual(page.show_dialog.call_count, 3)

    def test_failed_or_invalid_expense_does_not_show_snackbar(self):
        self.repo.set_budget(100)
        page = Mock(spec=ft.Page)
        app = ExpenseTracker(page, self.repo)
        app.build()
        app.amount.value = "invalid"
        app.add_expense(None)
        app.amount.value = "100"
        with patch.object(self.repo, "add", side_effect=sqlite3.OperationalError("locked")), \
                self.assertLogs(level="ERROR"):
            app.add_expense(None)
        page.show_dialog.assert_not_called()
        self.assertEqual(self.repo.list_all(), [])

    def test_recurring_charge_once_per_month_and_year_rollover(self):
        self.repo.add(129999, "Cloud", date(2025, 12, 31), "Hosting", recurring=True)
        original = self.repo.list_all()[0]
        self.assertIsNotNone(original.subscription_id)
        self.assertEqual(self.repo.generate_recurring_expenses(date(2025, 12, 31)), 0)
        self.assertEqual(self.repo.generate_recurring_expenses(date(2026, 1, 18)), 1)
        charge = self.repo.list_all()[0]
        self.assertEqual(charge.spent_on, date(2026, 1, 1))
        self.assertEqual(charge.subscription_id, original.subscription_id)
        self.assertEqual(charge.amount_paise, 129999)
        self.assertEqual(charge.description, "Hosting")
        reopened = ExpenseRepository(self.path)
        self.assertEqual(reopened.generate_recurring_expenses(date(2026, 1, 31)), 0)
        self.assertEqual(reopened.generate_recurring_expenses(date(2026, 2, 28)), 1)
        self.assertEqual(len(reopened.list_all()), 3)
        self.assertEqual(len(reopened.list_subscriptions()), 1)

    def test_recurring_skips_missed_months_and_future_start_dates(self):
        self.repo.add(100, "AI", date(2024, 1, 31), "AI", recurring=True)
        self.repo.add(200, "Cloud", date(2024, 6, 15), "Future", recurring=True)
        self.repo.add(300, "Food", date(2024, 1, 1), "One-off")
        self.assertEqual(self.repo.generate_recurring_expenses(date(2024, 3, 20)), 1)
        rows = self.repo.list_all()
        self.assertFalse(any(row.spent_on.month == 2 for row in rows))
        march = [row for row in rows if row.spent_on == date(2024, 3, 1)]
        self.assertEqual([row.category for row in march], ["AI"])
        self.assertEqual(len(rows), 4)

    def test_identical_subscriptions_remain_independent(self):
        for _ in range(2):
            self.repo.add(100, "AI", date(2026, 8, 1), "Seats", recurring=True)
        self.assertEqual(len(self.repo.list_subscriptions()), 2)
        self.assertEqual(self.repo.generate_recurring_expenses(date(2026, 9, 1)), 2)
        self.assertEqual(self.repo.generate_recurring_expenses(date(2026, 9, 2)), 0)
        self.assertEqual(len(self.repo.list_all()), 4)

    def test_deleted_charge_stays_deleted_and_stop_keeps_history(self):
        self.repo.add(100, "AI", date(2026, 8, 15), "Plan", recurring=True)
        self.repo.generate_recurring_expenses(date(2026, 9, 12))
        self.repo.delete(self.repo.list_all()[0].id)
        self.assertEqual(self.repo.generate_recurring_expenses(date(2026, 9, 20)), 0)
        self.assertEqual(len(self.repo.list_subscriptions()), 1)
        self.assertEqual(self.repo.generate_recurring_expenses(date(2026, 10, 1)), 1)
        history = self.repo.list_all()
        self.repo.stop_subscription(self.repo.list_subscriptions()[0].id)
        reopened = ExpenseRepository(self.path)
        self.assertEqual(reopened.list_subscriptions(), [])
        self.assertEqual(reopened.generate_recurring_expenses(date(2026, 11, 1)), 0)
        self.assertEqual(reopened.list_all(), history)

    def test_concurrent_startups_do_not_duplicate_charges(self):
        self.repo.add(100, "Cloud", date(2026, 8, 31), "Cloud", recurring=True)
        def launch(_):
            return ExpenseRepository(self.path).generate_recurring_expenses(date(2026, 9, 12))
        with ThreadPoolExecutor(max_workers=2) as executor:
            generated = list(executor.map(launch, range(2)))
        self.assertEqual(sorted(generated), [0, 1])
        self.assertEqual(len(self.repo.list_all()), 2)

    def test_recurring_migration_preserves_old_data(self):
        legacy_path = self.path.with_name("legacy.db")
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE expenses(id INTEGER PRIMARY KEY, amount_paise INTEGER,
                    category TEXT, spent_on TEXT, description TEXT);
                INSERT INTO expenses VALUES (7, 250050, 'Food', '2026-08-31', 'Old expense');
                CREATE TABLE budget(id INTEGER PRIMARY KEY, amount_paise INTEGER);
                INSERT INTO budget VALUES (1, 500000);
                CREATE TABLE preferences(id INTEGER PRIMARY KEY, theme TEXT);
                INSERT INTO preferences VALUES (1, 'dark');
            """)
        migrated = ExpenseRepository(legacy_path)
        old = migrated.list_all()[0]
        self.assertEqual((old.id, old.amount_paise, old.description), (7, 250050, "Old expense"))
        self.assertIsNone(old.subscription_id)
        self.assertEqual(migrated.get_budget(), 500000)
        self.assertEqual(migrated.get_theme(), "dark")
        self.assertEqual(migrated.generate_recurring_expenses(date(2026, 9, 1)), 0)
        self.assertEqual(ExpenseRepository(legacy_path).list_all(), [old])

    def test_recurring_insertion_and_generation_are_atomic(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_expense BEFORE INSERT ON expenses
                BEGIN SELECT RAISE(ABORT, 'Test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.add(100, "AI", date(2026, 8, 1), "AI", recurring=True)
        self.assertEqual(self.repo.list_subscriptions(), [])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER fail_expense")
        self.repo.add(100, "AI", date(2026, 8, 1), "AI", recurring=True)
        self.repo.add(200, "Cloud", date(2026, 8, 1), "Cloud", recurring=True)
        original = self.repo.list_all()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("""CREATE TRIGGER fail_expense BEFORE INSERT ON expenses
                WHEN NEW.category = 'Cloud' BEGIN SELECT RAISE(ABORT, 'Test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.generate_recurring_expenses(date(2026, 9, 1))
        self.assertEqual(self.repo.list_all(), original)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TRIGGER fail_expense")
        self.assertEqual(self.repo.generate_recurring_expenses(date(2026, 9, 1)), 2)

    def test_main_generates_subscriptions_before_building_dashboard(self):
        self.repo.add(10000, "AI", date(2026, 8, 15), "AI plan", recurring=True)
        page = Mock(spec=ft.Page)
        page.window = Mock()
        app = ExpenseTracker(page, self.repo)
        generate = self.repo.generate_recurring_expenses
        with patch("main.ExpenseRepository", return_value=self.repo), \
                patch("main.ExpenseTracker", return_value=app), \
                patch.object(self.repo, "generate_recurring_expenses",
                             side_effect=lambda: generate(date(2026, 9, 12))):
            main(page)
        self.assertEqual(app.total.value, "₹200.00")
        self.assertEqual(app.budget_spent.value, "₹200.00")
        self.assertEqual(app.category_amounts["AI"].value, "₹200.00")
        self.assertEqual(app.subscription_count.value, "1 active")
        self.assertEqual(self.repo.list_all()[0].spent_on, date(2026, 9, 1))
        self.assertIn("Logged 1 subscription charge", app.status.value)


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
            app.toggle_theme(None)
            self.assertEqual(session.page.theme_mode, ft.ThemeMode.DARK)
            app.toggle_theme(None)
            self.assertEqual(session.page.theme_mode, ft.ThemeMode.LIGHT)
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
            app.budget_amount.value = "125"
            app.save_budget(None)
            previous_notice = app.expense_snackbar
            app.amount.value = "1"
            app.add_expense(None)
            warning_notice = app.expense_snackbar
            self.assertFalse(previous_notice.open)
            self.assertTrue(warning_notice.open)
            self.assertEqual(warning_notice.content.value,
                             "Warning: You have used 80% of your budget!")
            app.amount.value = "30"
            app.add_expense(None)
            self.assertFalse(warning_notice.open)
            self.assertTrue(app.expense_snackbar.open)
            self.assertEqual(app.expense_snackbar.content.value,
                             "Alert: You have exceeded your budget for this month!")
            self.assertTrue(connection.send_message.called)
            app.recurring.value = True
            app.amount.value = "5"
            app.category.value = "Cloud"
            app.description.value = "Monthly hosting"
            app.add_expense(None)
            self.assertFalse(app.recurring.value)
            self.assertEqual(app.subscription_count.value, "1 active")
            subscription = repository.list_subscriptions()[0]
            self.assertEqual(subscription.description, "Monthly hosting")
            self.assertIsNotNone(repository.list_all()[0].subscription_id)
            app.stop_subscription(subscription)
            self.assertEqual(app.subscription_count.value, "0 active")


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
