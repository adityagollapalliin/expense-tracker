# Expense Tracker with Budget

All application code is in `main.py`. Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Flet may download its macOS desktop runtime on the first launch. Subsequent
launches use the installed runtime and local SQLite database.

- **Expenses:** Add an amount, category, date, and optional description. View
  expenses newest first and use the trash icon to delete an entry permanently.
- **Budget:** Enter your total available funds and click **Save budget** (or press
  Return). Saving replaces the previous total; it does not add to it. The dashboard
  shows Total Budget, Total Spent, Remaining Balance, and budget utilization.
- The budget applies to **all recorded expenses across all dates**. Adding or
  deleting an expense updates both screens immediately. Existing expenses count
  toward the budget as soon as it is set.
- Enter amounts without commas, with up to two decimal places. Expenses must be
  positive; a budget can be zero. Negative balances and utilization above 100%
  indicate overspending. The progress bar is capped at 100%. For a zero budget,
  the bar is empty with no spending and full when spending exceeds zero; no
  percentage is calculated. Before a budget is set, an explicit message prompts
  you to set one.

Money is stored as integer paise and displayed with Indian digit grouping,
such as `₹1,23,456.78`. `expenses.db` lives beside `main.py`. On startup, an
additive migration creates a single-record `budget` table if needed; existing
expenses are preserved. Budget changes persist when you close and reopen the app.
Close the app before copying `expenses.db` for a backup.

The pinned Flet 0.86.5 dependency uses
[Tabs, TabBar, and TabBarView](https://flet.dev/docs/controls/tabs/).
Each tab scrolls independently within the available window height.

Run the automated checks (temporary databases only):

```sh
python -m unittest discover -s tests -v
```
