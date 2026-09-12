"""A local Flet expense tracker. Run with: python main.py"""

from __future__ import annotations

import asyncio
import csv
import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import flet as ft
import flet_charts as fch

CATEGORIES = ("Cloud", "AI", "Utilities", "Fuel", "Repairs", "Food", "General")
DB_PATH = Path(__file__).resolve().with_name("expenses.db")
MAX_AMOUNT_PAISE = 99_999_999_999  # ₹99,99,99,999.99 per expense
TEAL = "#087F72"
INK = ft.Colors.ON_SURFACE
MUTED = ft.Colors.ON_SURFACE_VARIANT
CATEGORY_COLORS = {
    "Cloud": "#3976C6", "AI": "#8957B8", "Utilities": "#D19422",
    "Fuel": "#D16A3C", "Repairs": "#BD5274", "Food": TEAL, "General": "#647773",
}


def app_theme(*, dark: bool) -> ft.Theme:
    """Use semantic colors so existing controls adapt without being rebuilt."""
    return ft.Theme(
        color_scheme_seed=TEAL,
        color_scheme=ft.ColorScheme(
            primary="#7ED7C5" if dark else TEAL,
            on_primary="#00382E" if dark else "#FFFFFF",
            primary_container="#214A40" if dark else "#DDEFE9",
            on_primary_container="#ABEFDD" if dark else "#005047",
            surface="#101C1A" if dark else "#F0F5F2",
            surface_container_lowest="#172522" if dark else "#FFFFFF",
            surface_container_low="#1F302C" if dark else "#F6F9F7",
            on_surface="#E2EEE9" if dark else "#173B37",
            on_surface_variant="#ADC2B9" if dark else "#647773",
            outline_variant="#3D534B" if dark else "#E1EAE7",
            error="#FFB4AB" if dark else "#B3261E",
        ),
    )


def parse_amount(value: str, *, allow_zero: bool = False) -> int:
    """Validate decimal rupees and convert to exact integer paise."""
    value = value.strip()
    if len(value) > 12 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("Enter an amount like 250 or 250.50 (no commas).")
    paise = int(Decimal(value) * 100)
    minimum = 0 if allow_zero else 1
    if not minimum <= paise <= MAX_AMOUNT_PAISE:
        raise ValueError(f"Enter an amount from {format_inr(minimum)} "
                         "to ₹99,99,99,999.99.")
    return paise


def format_inr(paise: int) -> str:
    """Format money using Indian digit grouping without locale dependencies."""
    rupees, fraction = divmod(abs(paise), 100)
    digits = str(rupees)
    groups = [digits[-3:]]
    remaining = digits[:-3]
    while remaining:
        groups.insert(0, remaining[-2:])
        remaining = remaining[:-2]
    return f"{'−' if paise < 0 else ''}₹{','.join(groups)}.{fraction:02d}"


def parse_date(value: str) -> date:
    value = value.strip()
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use YYYY-MM-DD, or choose a date with the calendar.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter a valid calendar date.") from None


@dataclass(frozen=True)
class Expense:
    id: int
    amount_paise: int
    category: str
    spent_on: date
    description: str


def category_totals(expenses: list[Expense]) -> dict[str, int]:
    """Group expenses in category order, keeping exact totals in paise."""
    totals = dict.fromkeys(CATEGORIES, 0)
    for expense in expenses:
        totals[expense.category] += expense.amount_paise
    return totals


class ExpenseRepository:
    """SQLite persistence; short-lived connections also work across UI threads."""

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        categories_sql = ", ".join(f"'{category}'" for category in CATEGORIES)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS expenses (
                    id INTEGER PRIMARY KEY,
                    amount_paise INTEGER NOT NULL
                        CHECK(amount_paise > 0 AND amount_paise <= {MAX_AMOUNT_PAISE}),
                    category TEXT NOT NULL CHECK(category IN ({categories_sql})),
                    spent_on TEXT NOT NULL,
                    description TEXT NOT NULL CHECK(length(description) <= 200)
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS expenses_date_idx "
                "ON expenses(spent_on DESC, id DESC)"
            )
            # Additive migration: existing expense records remain untouched.
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS budget (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    amount_paise INTEGER NOT NULL CHECK(
                        typeof(amount_paise) = 'integer' AND
                        amount_paise >= 0 AND amount_paise <= {MAX_AMOUNT_PAISE}
                    )
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    theme TEXT NOT NULL CHECK(theme IN ('light', 'dark'))
                )"""
            )

    def get_theme(self) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT theme FROM preferences WHERE id = 1").fetchone()
        return row["theme"] if row else "light"

    def set_theme(self, theme: str) -> None:
        if theme not in ("light", "dark"):
            raise ValueError("Theme must be light or dark.")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO preferences(id, theme) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET theme = excluded.theme", (theme,),
            )

    def get_budget(self) -> int | None:
        """Return the all-time budget in paise, or None before it is set."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT amount_paise FROM budget WHERE id = 1"
            ).fetchone()
        return row["amount_paise"] if row else None

    def set_budget(self, amount_paise: int) -> None:
        if type(amount_paise) is not int or not 0 <= amount_paise <= MAX_AMOUNT_PAISE:
            raise ValueError("Invalid budget amount.")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO budget(id, amount_paise) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET amount_paise = excluded.amount_paise",
                (amount_paise,),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def add(self, amount_paise: int, category: str, spent_on: date,
            description: str) -> None:
        if type(amount_paise) is not int or not 0 < amount_paise <= MAX_AMOUNT_PAISE:
            raise ValueError("Invalid expense amount.")
        if category not in CATEGORIES:
            raise ValueError("Choose one of the available categories.")
        if type(spent_on) is not date:
            raise ValueError("Invalid expense date.")
        description = description.strip()
        if len(description) > 200:
            raise ValueError("Keep the description to 200 characters or fewer.")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO expenses(amount_paise, category, spent_on, description) "
                "VALUES (?, ?, ?, ?)",
                (amount_paise, category, spent_on.isoformat(), description),
            )

    def list_all(self) -> list[Expense]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM expenses ORDER BY spent_on DESC, id DESC"
            ).fetchall()
        return [Expense(row["id"], row["amount_paise"], row["category"],
                        date.fromisoformat(row["spent_on"]), row["description"])
                for row in rows]

    def delete(self, expense_id: int) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))


def export_expenses_csv(repository: ExpenseRepository, path: Path) -> int:
    """Export a fresh database snapshot, with numeric INR amounts for spreadsheets."""
    expenses = repository.list_all()
    # UTF-8 BOM helps spreadsheet apps detect Unicode; csv handles quotes/newlines.
    with path.open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["ID", "Amount", "Category", "Date", "Description"])
        for expense in expenses:
            writer.writerow([
                expense.id,
                f"{expense.amount_paise // 100}.{expense.amount_paise % 100:02d}",
                expense.category, expense.spent_on.isoformat(), expense.description,
            ])
    return len(expenses)


class ExpenseTracker:
    """Compose the view and coordinate form actions with the repository."""

    def __init__(self, page: ft.Page, repository: ExpenseRepository):
        self.page = page
        self.repository = repository
        self.theme_toggle = ft.IconButton(
            icon=ft.Icons.DARK_MODE_OUTLINED, tooltip="Switch to dark mode",
            icon_color=ft.Colors.PRIMARY, on_click=self.toggle_theme,
        )
        self.amount = ft.TextField(
            label="Amount", prefix="₹ ", hint_text="0.00",
            keyboard_type=ft.KeyboardType.NUMBER, autofocus=True,
        )
        self.category = ft.Dropdown(
            label="Category", value="General",
            options=[ft.DropdownOption(key=name, text=name) for name in CATEGORIES],
        )
        self.spent_on = ft.TextField(
            label="Date", value=date.today().isoformat(), hint_text="YYYY-MM-DD",
            expand=True,
        )
        self.description = ft.TextField(
            label="Description (optional)", hint_text="What was it for?",
            multiline=True, min_lines=2, max_lines=3, max_length=200,
        )
        self.picker = ft.DatePicker(
            first_date=datetime(1900, 1, 1), last_date=datetime(2100, 12, 31),
            value=datetime.combine(date.today(), datetime.min.time()),
            on_change=self.pick_date,
        )
        self.total = ft.Text(format_inr(0), size=38, weight=ft.FontWeight.BOLD,
                             color=ft.Colors.WHITE, selectable=True)
        self.count = ft.Text("0 expenses recorded", color="#C4E9E2", size=13)
        self.history_count = ft.Text("", color=MUTED, size=12)
        self.history = ft.ListView(height=470, spacing=10)
        self.status = ft.Text("", size=13, visible=False)
        self.expense_snackbar: ft.SnackBar | None = None
        self.file_picker = ft.FilePicker()
        self.export_button = ft.OutlinedButton(
            content="Export to CSV", icon=ft.Icons.DOWNLOAD_OUTLINED,
            on_click=self.export_csv,
            tooltip="Export all saved expenses to a CSV file",
            style=ft.ButtonStyle(color=ft.Colors.PRIMARY),
        )
        self.budget_amount = ft.TextField(
            label="Total budget", prefix="₹ ", hint_text="e.g. 25000.00",
            keyboard_type=ft.KeyboardType.NUMBER, on_submit=self.save_budget,
        )
        self.budget_total = ft.Text(size=28, weight=ft.FontWeight.BOLD,
                                    color=INK, selectable=True)
        self.budget_spent = ft.Text(size=28, weight=ft.FontWeight.BOLD,
                                    color=INK, selectable=True)
        self.remaining = ft.Text(size=28, weight=ft.FontWeight.BOLD,
                                 color=ft.Colors.PRIMARY, selectable=True)
        self.utilization = ft.Text(color=MUTED)
        self.progress = ft.ProgressBar(
            value=0, color=ft.Colors.PRIMARY, bgcolor=ft.Colors.PRIMARY_CONTAINER, bar_height=12,
            border_radius=6, semantics_label="Budget utilized",
        )
        self.spending_chart = fch.PieChart(
            sections=[], width=260, height=260, center_space_radius=55,
            sections_space=2, start_degree_offset=270, visible=False,
        )
        self.chart_empty = ft.Text(
            "No spending yet.\nAdd an expense to see your category breakdown.",
            color=MUTED, text_align=ft.TextAlign.CENTER,
        )
        self.category_amounts = {
            category: ft.Text(format_inr(0), color=INK,
                              weight=ft.FontWeight.W_600, selectable=True)
            for category in CATEGORIES
        }
        self.category_percentages = {
            category: ft.Text("0.0%", color=MUTED, size=12)
            for category in CATEGORIES
        }

    @staticmethod
    def panel(content: ft.Control, col: dict) -> ft.Container:
        return ft.Container(
            content=content, col=col, bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            padding=24, border_radius=20, border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
        )

    def build(self) -> None:
        try:
            self.apply_theme(ft.ThemeMode(self.repository.get_theme()))
            theme_load_failed = False
        except sqlite3.Error:
            logging.exception("Could not load theme preference")
            self.apply_theme(ft.ThemeMode.LIGHT)
            theme_load_failed = True
        form = self.panel(
            ft.Column([
                ft.Text("Add an expense", size=22, weight=ft.FontWeight.BOLD,
                        color=INK),
                ft.Text("A small entry. A clearer picture.", color=MUTED, size=13),
                ft.Container(height=4), self.amount, self.category,
                ft.Row([self.spent_on, ft.IconButton(
                    icon=ft.Icons.CALENDAR_MONTH_OUTLINED,
                    tooltip="Choose expense date", icon_color=ft.Colors.PRIMARY,
                    on_click=self.open_calendar,
                )]),
                self.description,
                ft.FilledButton(
                    content="Add expense", icon=ft.Icons.ADD_ROUNDED,
                    width=float("inf"), height=48, on_click=self.add_expense,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.PRIMARY, color=ft.Colors.ON_PRIMARY),
                ),
            ], spacing=16), {"xs": 12, "md": 5, "lg": 4},
        )
        history = self.panel(
            ft.Column([
                ft.Text("Expense history", size=22, weight=ft.FontWeight.BOLD,
                        color=INK),
                ft.Row([ft.Text("Newest first", size=13, color=MUTED),
                        self.history_count],
                       alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                self.export_button,
                ft.Divider(color=ft.Colors.OUTLINE_VARIANT), self.history,
            ], spacing=14), {"xs": 12, "md": 7, "lg": 8},
        )
        expenses_screen = ft.Column([
            ft.Container(
                bgcolor=TEAL, border_radius=20, padding=28,
                content=ft.Column([
                    ft.Text("TOTAL SPENT · ALL TIME", size=12,
                            weight=ft.FontWeight.W_600, color="#C4E9E2"),
                    self.total, self.count,
                ], spacing=9),
            ),
            ft.ResponsiveRow([form, history], spacing=20, run_spacing=20),
        ], spacing=24, scroll=ft.ScrollMode.AUTO)
        self.tabs = ft.Tabs(
            length=2, selected_index=0, expand=True,
            content=ft.Column([
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="Expenses", icon=ft.Icons.RECEIPT_LONG_OUTLINED),
                        ft.Tab(label="Budget", icon=ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED),
                    ],
                    label_color=ft.Colors.PRIMARY, indicator_color=ft.Colors.PRIMARY,
                    unselected_label_color=MUTED,
                ),
                ft.TabBarView(
                    controls=[expenses_screen, self.build_budget_screen()], expand=True,
                ),
            ], spacing=20, expand=True),
        )
        self.page.add(ft.Container(
            width=1200, expand=True,
            padding=ft.Padding.symmetric(horizontal=24, vertical=28),
            content=ft.Column([
                ft.Row([
                    ft.Container(
                        content=ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
                                        color=ft.Colors.PRIMARY, size=27),
                        bgcolor=ft.Colors.PRIMARY_CONTAINER, padding=13, border_radius=16,
                    ),
                    ft.Column([
                        ft.Text("Expense Tracker", size=28,
                                weight=ft.FontWeight.BOLD, color=INK),
                        ft.Text("Every rupee, accounted for.", color=MUTED, size=14),
                    ], spacing=3, expand=True),
                    self.theme_toggle,
                ], spacing=14),
                self.status,
                self.tabs,
                ft.Text("Saved on this Mac · SQLite local storage",
                        size=12, color=MUTED),
            ], spacing=24, expand=True),
        ))
        self.refresh(update_budget_input=True)
        if theme_load_failed:
            self.notify("Could not load your theme preference. Using light mode.", error=True)

    def apply_theme(self, mode: ft.ThemeMode) -> None:
        self.page.theme_mode = mode
        dark = mode == ft.ThemeMode.DARK
        self.theme_toggle.icon = (
            ft.Icons.LIGHT_MODE_OUTLINED if dark else ft.Icons.DARK_MODE_OUTLINED
        )
        self.theme_toggle.tooltip = "Switch to light mode" if dark else "Switch to dark mode"

    def toggle_theme(self, _event: ft.Event) -> None:
        mode = (ft.ThemeMode.LIGHT if self.page.theme_mode == ft.ThemeMode.DARK
                else ft.ThemeMode.DARK)
        self.apply_theme(mode)
        try:
            self.repository.set_theme(mode.value)
        except sqlite3.Error:
            logging.exception("Could not save theme preference")
            self.notify("Theme changed for this session, but the preference could not "
                        "be saved. Try toggling again to save it.", error=True)
            return
        self.page.update()

    def build_budget_screen(self) -> ft.Column:
        summary_cards = [
            self.panel(ft.Column([
                ft.Text(label, color=MUTED, size=13), value,
            ], spacing=12), {"xs": 12, "md": 4})
            for label, value in [
                ("Total Budget", self.budget_total),
                ("Total Spent", self.budget_spent),
                ("Remaining Balance", self.remaining),
            ]
        ]
        return ft.Column([
            ft.Text("Your budget", size=24, weight=ft.FontWeight.BOLD, color=INK),
            ft.Text("One total budget for all recorded expenses, across all dates.",
                    color=MUTED),
            ft.ResponsiveRow(summary_cards, spacing=16, run_spacing=16),
            self.panel(ft.Column([
                ft.Text("Budget utilization", size=18, weight=ft.FontWeight.W_600,
                        color=INK),
                self.progress, self.utilization,
            ], spacing=16), {"xs": 12}),
            self.build_spending_breakdown(),
            self.panel(ft.Column([
                ft.Text("Set or update your budget", size=20,
                        weight=ft.FontWeight.BOLD, color=INK),
                ft.Text("This replaces your total budget. Enter 0 if no funds are available.",
                        color=MUTED, size=13),
                self.budget_amount,
                ft.FilledButton(
                    content="Save budget", icon=ft.Icons.SAVE_OUTLINED,
                    height=48, on_click=self.save_budget,
                    style=ft.ButtonStyle(bgcolor=ft.Colors.PRIMARY, color=ft.Colors.ON_PRIMARY),
                ),
            ], spacing=16), {"xs": 12}),
        ], spacing=20, scroll=ft.ScrollMode.AUTO)

    def build_spending_breakdown(self) -> ft.Container:
        legend = ft.Column([
            ft.Row([
                ft.Container(width=12, height=12, border_radius=6,
                             bgcolor=CATEGORY_COLORS[category]),
                ft.Column([
                    ft.Text(category, color=INK), self.category_percentages[category],
                ], spacing=2, expand=True),
                self.category_amounts[category],
            ], spacing=12)
            for category in CATEGORIES
        ], spacing=12)
        return self.panel(ft.Column([
            ft.Text("Spending by category", size=20,
                    weight=ft.FontWeight.BOLD, color=INK),
            ft.Text("All time · Percentages show each category’s share of total spending.",
                    color=MUTED, size=13),
            ft.ResponsiveRow([
                ft.Container(
                    content=ft.Column(
                        [self.spending_chart, self.chart_empty],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                    height=300, col={"xs": 12, "md": 5},
                ),
                ft.Container(content=legend, col={"xs": 12, "md": 7}),
            ], spacing=24, run_spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ], spacing=16), {"xs": 12})

    def update_spending_breakdown(self, expenses: list[Expense]) -> None:
        totals = category_totals(expenses)
        total = sum(totals.values())
        self.spending_chart.sections = [
            fch.PieChartSection(
                key=category, value=amount, color=CATEGORY_COLORS[category],
                radius=45, title="",
            )
            for category, amount in totals.items() if amount > 0
        ]
        self.spending_chart.visible = total > 0
        self.chart_empty.visible = total == 0
        for category, amount in totals.items():
            percentage = Decimal(amount) * 100 / Decimal(total) if total else Decimal(0)
            label = "<0.1%" if 0 < percentage < Decimal("0.1") else f"{percentage:.1f}%"
            self.category_amounts[category].value = format_inr(amount)
            self.category_percentages[category].value = label

    def save_budget(self, _event: ft.Event) -> None:
        self.budget_amount.error = None
        self.status.visible = False
        try:
            amount = parse_amount(self.budget_amount.value or "", allow_zero=True)
        except ValueError as error:
            self.budget_amount.error = str(error)
            self.page.update()
            return
        try:
            self.repository.set_budget(amount)
        except sqlite3.Error:
            logging.exception("Could not save budget")
            self.notify("Could not save the budget. Please try again.", error=True)
            return
        if self.refresh(update_budget_input=True):
            self.notify("Budget saved.")

    def update_budget_summary(self, budget: int | None, spent: int) -> None:
        remaining = (budget or 0) - spent
        self.budget_total.value = format_inr(budget or 0)
        self.budget_spent.value = format_inr(spent)
        self.remaining.value = format_inr(remaining)
        self.remaining.color = ft.Colors.ERROR if remaining < 0 else ft.Colors.PRIMARY
        self.progress.color = ft.Colors.ERROR if budget is not None and spent > budget else ft.Colors.PRIMARY
        if budget is None:
            self.progress.value = 0
            message = "No budget set yet. Save a budget to track utilization."
        elif budget == 0:
            self.progress.value = 1 if spent else 0
            message = "No funds available in your budget."
            if spent:
                message += f" Over budget by {format_inr(spent)}."
        else:
            ratio = spent / budget
            self.progress.value = min(ratio, 1.0)
            # Keep the true percentage even when the visual bar is full.
            percentage = Decimal(spent) * 100 / Decimal(budget)
            message = f"{percentage:.1f}% of budget utilized"
            if remaining < 0:
                message += f" · Over budget by {format_inr(-remaining)}"
            elif remaining == 0:
                message += " · Budget fully utilized"
            else:
                message += f" · {format_inr(remaining)} available"
        self.utilization.value = message
        # Flet's semantics_value is numeric; descriptive text belongs in the label.
        self.progress.semantics_label = message

    async def export_csv(self, _event: ft.Event) -> None:
        if self.export_button.disabled:
            return
        self.export_button.disabled = True
        self.status.visible = False
        self.page.update()
        try:
            selected_path = await self.file_picker.save_file(
                dialog_title="Export expenses to CSV",
                file_name=f"expenses-{date.today().isoformat()}.csv",
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["csv"],
            )
            if not selected_path:
                self.notify("Export cancelled.")
                return
            # Query after the dialog closes, and keep disk I/O off the UI loop.
            count = await asyncio.to_thread(
                export_expenses_csv, self.repository, Path(selected_path),
            )
            self.notify(f"Exported {count} expense{'s' if count != 1 else ''} "
                        f"to {selected_path}")
        except (sqlite3.Error, OSError, ft.FletException, TimeoutError):
            logging.exception("Could not export expenses")
            self.notify("Could not export expenses. Check the save location "
                        "and try again.", error=True)
        finally:
            self.export_button.disabled = False
            self.page.update()

    def notify(self, message: str, error: bool = False) -> None:
        self.status.value = message
        self.status.color = ft.Colors.ERROR if error else ft.Colors.PRIMARY
        self.status.visible = True
        self.page.update()

    def show_expense_snackbar(self, budget: int | None, spent: int) -> None:
        """Notify after a saved expense using the same totals as the dashboard."""
        if budget is not None and spent > budget:
            message = "Alert: You have exceeded your budget for this month!"
            background, foreground = "#B3261E", "#FFFFFF"
        elif budget is not None and budget > 0 and spent * 100 >= budget * 80:
            # Integer paise comparisons avoid rounding around the 80% boundary.
            message = "Warning: You have used 80% of your budget!"
            background, foreground = "#FFD54F", "#3A2C00"
        else:
            message = "Expense added."
            background, foreground = ft.Colors.PRIMARY, ft.Colors.ON_PRIMARY
        # Replace an earlier notice so a new over-budget alert isn't queued behind it.
        if self.expense_snackbar is not None and self.expense_snackbar.open:
            self.expense_snackbar.open = False
            self.page.update()
        self.expense_snackbar = ft.SnackBar(
            content=ft.Text(message, color=foreground), bgcolor=background,
            behavior=ft.SnackBarBehavior.FLOATING, duration=6000,
            show_close_icon=True, close_icon_color=foreground, persist=False,
        )
        self.page.show_dialog(self.expense_snackbar)

    def open_calendar(self, _event: ft.Event) -> None:
        try:
            selected = parse_date(self.spent_on.value or "")
            if date(1900, 1, 1) <= selected <= date(2100, 12, 31):
                self.picker.value = datetime.combine(selected, datetime.min.time())
        except ValueError:
            pass
        self.page.show_dialog(self.picker)

    def pick_date(self, _event: ft.Event) -> None:
        if self.picker.value:
            self.spent_on.value = self.picker.value.date().isoformat()
            self.spent_on.error = None
            self.page.update()

    def add_expense(self, _event: ft.Event) -> None:
        self.amount.error = self.spent_on.error = None
        self.description.error = None
        self.status.visible = False
        valid = True
        try:
            amount = parse_amount(self.amount.value or "")
        except ValueError as error:
            self.amount.error = str(error)
            valid = False
        try:
            spent_on = parse_date(self.spent_on.value or "")
        except ValueError as error:
            self.spent_on.error = str(error)
            valid = False
        description = (self.description.value or "").strip()
        if len(description) > 200:
            self.description.error = "Use 200 characters or fewer."
            valid = False
        if not valid:
            self.page.update()
            return
        try:
            self.repository.add(amount, self.category.value, spent_on, description)
        except ValueError as error:
            self.notify(str(error), error=True)
            return
        except sqlite3.Error:
            logging.exception("Could not save expense")
            self.notify("Could not save the expense. Check that the app folder is writable, "
                        "then try again.", error=True)
            return
        self.amount.value = ""
        self.description.value = ""
        if self.refresh(expense_added=True):
            self.notify("Expense added.")

    def delete_expense(self, expense: Expense) -> None:
        try:
            self.repository.delete(expense.id)
        except sqlite3.Error:
            logging.exception("Could not delete expense")
            self.notify("Could not delete the expense. Please try again.", error=True)
            return
        if self.refresh():
            self.notify(f"Deleted {format_inr(expense.amount_paise)} expense.")

    def expense_row(self, expense: Expense) -> ft.Container:
        return ft.Container(
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOW, border_radius=12, padding=14,
            content=ft.Column([
                ft.Row([
                    ft.Text(expense.category, weight=ft.FontWeight.W_600,
                            color=INK, expand=True),
                    ft.Text(format_inr(expense.amount_paise),
                            weight=ft.FontWeight.BOLD, color=INK, selectable=True),
                    ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE_ROUNDED, icon_color=MUTED,
                        icon_size=20,
                        tooltip=f"Delete {expense.category} expense of "
                                f"{format_inr(expense.amount_paise)}",
                        on_click=lambda _event: self.delete_expense(expense),
                    ),
                ], spacing=8),
                ft.Text(expense.description or "No description", size=13,
                        color=MUTED, selectable=True),
                ft.Text(expense.spent_on.strftime("%d %b %Y"), size=12, color=MUTED),
            ], spacing=4),
        )

    def refresh(self, *, update_budget_input: bool = False,
                expense_added: bool = False) -> bool:
        try:
            expenses = self.repository.list_all()
            budget = self.repository.get_budget()
        except sqlite3.Error:
            logging.exception("Could not load expenses or budget")
            self.notify("Could not load expenses or budget. Restart the app to retry.",
                        error=True)
            return False
        spent = sum(item.amount_paise for item in expenses)
        self.total.value = format_inr(spent)
        self.update_budget_summary(budget, spent)
        self.update_spending_breakdown(expenses)
        if update_budget_input:
            self.budget_amount.value = (
                f"{budget // 100}.{budget % 100:02d}" if budget is not None else ""
            )
        count = len(expenses)
        self.count.value = f"{count} expense{'s' if count != 1 else ''} recorded"
        self.history_count.value = f"{count} total"
        self.history.controls = [self.expense_row(item) for item in expenses] or [
            ft.Container(
                padding=ft.Padding.symmetric(vertical=70),
                content=ft.Column([
                    ft.Icon(ft.Icons.RECEIPT_LONG_OUTLINED, size=44, color=ft.Colors.PRIMARY),
                    ft.Text("Your first expense starts here", size=18,
                            weight=ft.FontWeight.W_600, color=INK,
                            text_align=ft.TextAlign.CENTER),
                    ft.Text("Add an expense to see it in your history.", color=MUTED,
                            text_align=ft.TextAlign.CENTER),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14),
            )
        ]
        self.page.update()
        if expense_added:
            self.show_expense_snackbar(budget, spent)
        return True


def main(page: ft.Page) -> None:
    page.title = "Expense Tracker"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = app_theme(dark=False)
    page.dark_theme = app_theme(dark=True)
    page.bgcolor = ft.Colors.SURFACE
    page.padding = 0
    # TabBarView needs bounded height; each tab handles its own scrolling.
    page.scroll = None
    page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    page.window.width = 1180
    page.window.height = 900
    page.window.min_width = 420
    page.window.min_height = 650
    try:
        repository = ExpenseRepository()
    except sqlite3.Error:
        logging.exception("Could not open expense database")
        page.add(ft.Container(padding=32, content=ft.Text(
            "Could not open expenses.db. Make sure the app folder is writable, "
            "then restart the app.", color=ft.Colors.ERROR)))
        return
    ExpenseTracker(page, repository).build()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ft.run(main)
