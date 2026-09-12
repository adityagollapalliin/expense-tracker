# Expense Tracker

A local macOS expense tracker built with Python, Flet, and SQLite.

- **Expenses:** Add expenses, browse history newest first, and delete incorrect entries.
- **Budget:** Save a total budget and track spending, remaining balance, and utilization.
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
