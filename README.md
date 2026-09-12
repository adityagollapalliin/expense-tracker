# Expense Tracker

A modern local macOS expense tracker built with Python, Flet, and SQLite.
All application code is in `main.py`, with separate model, repository, and UI classes.

## Setup and run

Use Python 3.10 or newer. In Terminal, from this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python main.py
```

Flet may download its desktop runtime on the first launch, so keep an internet
connection available for setup and that first launch. Later launches use the
installed runtime and local database. To launch again:

```sh
cd /path/to/expense-tracker
source .venv/bin/activate
python main.py
```

## Using the app

- Enter a positive amount with up to two decimal places, such as `250.50`.
- Choose from **Cloud, AI, Utilities, Fuel, Repairs, Food, General**.
  General is selected initially.
- The date starts at today. Type `YYYY-MM-DD` or use the calendar button.
- Add an optional description of up to 200 characters, then click **Add expense**.
- The all-time total updates immediately. Scroll the history to view older entries;
  dates sort newest first, with the latest entry first when dates match.
- Click an entry's trash icon to delete it immediately. Deletion is permanent.

Amounts use Indian Rupees and Indian digit grouping, for example `₹1,23,456.78`.
Money is stored as integer paise to avoid floating-point rounding errors.

## Local data

`expenses.db` is created automatically beside `main.py`, regardless of the working
directory used to launch the app. Records persist after closing the app. Close the
app and copy this file to back up your data. SQLite is included with Python;
there is no database server or account to configure.

## Verification

```sh
python -m unittest discover -s tests -v
```

The UI uses the current [Flet dropdown](https://flet.dev/docs/controls/dropdown/)
and [date picker](https://flet.dev/docs/controls/datepicker/) APIs. The dependency
is pinned so that subsequent installs use the same API version.
