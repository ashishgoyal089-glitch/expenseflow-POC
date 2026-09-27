# ExpenseFlow API 

## What this is
A small expense submission and approval API. PoC, not production.
One userjourney: submit an expense, convert it to a base currency, approve or reject it. 

## Stack
- Python 3.14,FastAPI, Uvicorn- SQLAlchemyORM on SQLite (file: expenseflow.db) for the PoC
- httpx for theexternal FX rate call- pydantic v2for request and response models
- pytest fortests 

## Conventions
- Layout:app/main.py, app/db.py, app/models.py, app/schemas.py, app/routes.py
- Type hints onevery function. Docstrings on every endpoint.
- Money is stored as integer minor units (paise / cents), never float.
- Base currencyis INR. All amounts are normalised to base on write.
- Always use the Rupee symbol (₹) when denoting money in INR, e.g. ₹125.50.
- Use the Indian number format for thousands separators (lakh/crore grouping), e.g. ₹1,25,000.00 and ₹1,25,00,000.00.
- Never hardcode secrets. Read them from environment variables via python-dotenv. 

## Run and test
- Run:  python -m uvicorn app.main:app --reload
- Test: python-m pytest -q 

## Do not touch
- Do not edit.venv, .git, or expenseflow.db directly.
- Do not addnew third-party dependencies without telling me first.
- Do not invent endpoints that are not in the brief.