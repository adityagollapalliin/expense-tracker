"""A local Flet expense tracker. Run with: python main.py"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import flet as ft

CATEGORIES = ("Cloud", "AI", "Utilities", "Fuel", "Repairs", "Food", "General")
DB_PATH = Path(__file__).resolve().with_name("expenses.db")
MAX_AMOUNT_PAISE = 99_999_999_999  # ₹99,99,99,999.99 per expense
TEAL = "#087F72"
INK = "#173B37"
MUTED = "#647773"


def parse_amount(value: str) -> int:
    """Validate decimal rupees and convert to exact integer paise."""
    value = value.strip()
    if len(value) > 12 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError("Enter an amount like 250 or 250.50 (no commas).")
    paise = int(Decimal(value) * 100)
    if not 0 < paise <= MAX_AMOUNT_PAISE:
        raise ValueError("Enter an amount from ₹0.01 to ₹99,99,99,999.99.")
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


class ExpenseTracker:
    """Compose the view and coordinate form actions with the repository."""

    def __init__(self, page: ft.Page, repository: ExpenseRepository):
        self.page = page
        self.repository = repository
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

    @staticmethod
    def panel(content: ft.Control, col: dict) -> ft.Container:
        return ft.Container(
            content=content, col=col, bgcolor=ft.Colors.WHITE,
            padding=24, border_radius=20, border=ft.Border.all(1, "#E1EAE7"),
        )

    def build(self) -> None:
        form = self.panel(
            ft.Column([
                ft.Text("Add an expense", size=22, weight=ft.FontWeight.BOLD,
                        color=INK),
                ft.Text("A small entry. A clearer picture.", color=MUTED, size=13),
                ft.Container(height=4), self.amount, self.category,
                ft.Row([self.spent_on, ft.IconButton(
                    icon=ft.Icons.CALENDAR_MONTH_OUTLINED,
                    tooltip="Choose expense date", icon_color=TEAL,
                    on_click=self.open_calendar,
                )]),
                self.description,
                ft.FilledButton(
                    content="Add expense", icon=ft.Icons.ADD_ROUNDED,
                    width=float("inf"), height=48, on_click=self.add_expense,
                    style=ft.ButtonStyle(bgcolor=TEAL, color=ft.Colors.WHITE),
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
                ft.Divider(color="#E8EFEC"), self.history,
            ], spacing=14), {"xs": 12, "md": 7, "lg": 8},
        )
        self.page.add(ft.Container(
            width=1200, padding=ft.Padding.symmetric(horizontal=24, vertical=28),
            content=ft.Column([
                ft.Row([
                    ft.Container(
                        content=ft.Icon(ft.Icons.ACCOUNT_BALANCE_WALLET_OUTLINED,
                                        color=TEAL, size=27),
                        bgcolor="#DDEFE9", padding=13, border_radius=16,
                    ),
                    ft.Column([
                        ft.Text("Expense Tracker", size=28,
                                weight=ft.FontWeight.BOLD, color=INK),
                        ft.Text("Every rupee, accounted for.", color=MUTED, size=14),
                    ], spacing=3, expand=True),
                ], spacing=14),
                ft.Container(
                    bgcolor=TEAL, border_radius=20, padding=28,
                    content=ft.Column([
                        ft.Text("TOTAL SPENT · ALL TIME", size=12,
                                weight=ft.FontWeight.W_600, color="#C4E9E2"),
                        self.total, self.count,
                    ], spacing=9),
                ),
                self.status,
                ft.ResponsiveRow([form, history], spacing=20, run_spacing=20),
                ft.Text("Saved on this Mac · SQLite local storage",
                        size=12, color=MUTED),
            ], spacing=24),
        ))
        self.refresh()

    def notify(self, message: str, error: bool = False) -> None:
        self.status.value = message
        self.status.color = "#B3261E" if error else TEAL
        self.status.visible = True
        self.page.update()

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
        if self.refresh():
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
            bgcolor="#F6F9F7", border_radius=12, padding=14,
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

    def refresh(self) -> bool:
        try:
            expenses = self.repository.list_all()
        except sqlite3.Error:
            logging.exception("Could not load expenses")
            self.notify("Could not load expenses. Restart the app to retry.", error=True)
            return False
        self.total.value = format_inr(sum(item.amount_paise for item in expenses))
        count = len(expenses)
        self.count.value = f"{count} expense{'s' if count != 1 else ''} recorded"
        self.history_count.value = f"{count} total"
        self.history.controls = [self.expense_row(item) for item in expenses] or [
            ft.Container(
                padding=ft.Padding.symmetric(vertical=70),
                content=ft.Column([
                    ft.Icon(ft.Icons.RECEIPT_LONG_OUTLINED, size=44, color=TEAL),
                    ft.Text("Your first expense starts here", size=18,
                            weight=ft.FontWeight.W_600, color=INK,
                            text_align=ft.TextAlign.CENTER),
                    ft.Text("Add an expense to see it in your history.", color=MUTED,
                            text_align=ft.TextAlign.CENTER),
                ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=14),
            )
        ]
        self.page.update()
        return True


def main(page: ft.Page) -> None:
    page.title = "Expense Tracker"
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=TEAL)
    page.bgcolor = "#F0F5F2"
    page.padding = 0
    page.scroll = ft.ScrollMode.AUTO
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
            "then restart the app.", color="#B3261E")))
        return
    ExpenseTracker(page, repository).build()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ft.run(main)
