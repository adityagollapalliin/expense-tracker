# Expense Tracker

A local macOS expense tracker built with Python, Flet, and SQLite.

- **Expenses:** Add expenses, browse history newest first, and delete incorrect entries.
- **Budget:** Save a total budget and track spending, remaining balance, and utilization.
- **Budget alerts:** Expense additions show a yellow warning at 80–100% utilization
  and a red alert above 100%.
- **Analytics:** View a spending-by-category donut chart with ₹ totals and percentages.
- **CSV export:** Save all expenses through the native macOS save dialog.
- **Appearance:** Switch between light and dark mode using the header's sun/moon button.
  Your preference is remembered on the next launch.
- Amounts display in Indian Rupees (₹), and data persists locally in `expenses.db`.

## Run locally

Requires Python 3.10 or newer. From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

All application code is in `main.py`. See [SETUP.md](SETUP.md) for usage,
database migration, backup, and testing instructions.
