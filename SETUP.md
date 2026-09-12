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
- **Appearance:** Click the moon icon in the top right of the header for dark
  mode; click the sun icon to return to light mode. Both tabs, forms, panels,
  and status messages adapt immediately. Draft entries and the selected tab
  stay in place. The app starts in light mode until you choose otherwise, then
  remembers your choice in the SQLite `preferences` table on future launches.
- **Export to CSV:** Click the button above expense history, choose a file name
  and location in the macOS save dialog, and click Save. The export reads all
  currently saved expenses, newest first, with headers `ID, Amount, Category,
  Date, Description`. Amounts are numeric INR values with two decimal places
  (for example `1250.50`), and dates use `YYYY-MM-DD`. Unicode descriptions,
  commas, quotes, and line breaks are preserved. An empty database exports just
  the header row. Cancelling the dialog creates no file.
- **Budget:** Enter your total available funds and click **Save budget** (or press
  Return). Saving replaces the previous total; it does not add to it. The dashboard
  shows Total Budget, Total Spent, Remaining Balance, and budget utilization.
- **Spending by category:** The Budget tab includes a donut chart and a legend
  showing ₹ totals and each category's percentage of all-time spending. All seven
  categories stay in the legend, including those with zero spending. The chart
  updates when expenses are added or deleted and works without a saved budget.
  Before the first expense, an empty-state message replaces the chart.
- The budget applies to **all recorded expenses across all dates**. Adding or
  deleting an expense updates both screens immediately. Existing expenses count
  toward the budget as soon as it is set.
- **Budget alerts:** Each successful expense addition shows a SnackBar. From
  80% through exactly 100% utilization, it says "Warning: You have used 80% of
  your budget!" on yellow. Above 100%, it says "Alert: You have exceeded your
  budget for this month!" on red, and the budget progress bar is red as well.
  These messages repeat on additions within those ranges. The requested alert
  wording mentions a month, but calculations still use the existing all-time
  budget; no monthly reset or date filter is applied. With no budget or below
  80%, the notification says "Expense added." A saved zero budget triggers the
  red alert for any spending. Deletions and budget edits update the dashboard
  without triggering expense-added notifications.
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
The chart uses Flet's official
[flet-charts PieChart](https://flet.dev/docs/controls/charts/piechart/) package.
After updating an existing installation, install the new dependency and restart:

```sh
python -m pip install -r requirements.txt
python main.py
```

Run the automated checks (temporary databases only):

```sh
python -m unittest discover -s tests -v
```
