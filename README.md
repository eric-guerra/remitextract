# RemitExtract

Portfolio reference implementation of a 2024 project, built to show automated reading of customer **remittance advices**: the PDFs, emails and spreadsheets customers send to say which invoices a payment covers and why they paid less. The output feeds cash application.

**Stack:** Python 3.11, Anthropic Python SDK (Claude Opus 5.5, structured outputs into Pydantic models), Pydantic v2, Azure Blob Storage (managed identity).

## Pipeline (`remitextract/pipeline.py`)
```
remittances/incoming/<customer>/<file>   (email-ingest function / SFTP drop)
        │  extract → validate
        ├─▶ extracted/<customer>/<file>.json   auto-applied by the cash-app job
        └─▶ review/<customer>/<file>.json      AR review queue, with the reasons
original → processed/<customer>/<file>     (moved only after a result is written)
```
Unexpected errors (network, throttling) leave the blob in `incoming/` for the next run, so a document is never lost or half-processed.

## Extraction (`remitextract/extract.py`)
- **Structured outputs:**
  - `client.beta.messages.parse(output_format=Remittance, ...)` returns a validated `Remittance` model. That model holds the payer, the payment reference, date, currency and total, and one line per invoice with any deduction amount, a deduction reason enum, the customer's wording, and a **verbatim `source_quote`**.
  - PDFs and images go in as base64 `document`/`image` blocks; email bodies and CSVs as text documents.
- **Model setup:** `claude-opus-5-5` with adaptive thinking (its default) and `effort: "medium"`. Server-side fallbacks are on (`fallbacks: "default"`), and `refusal` and `max_tokens` stop reasons raise instead of returning partial data.
- **The model reads; code decides.** `validate()` gates auto-apply on deterministic checks:
  - The lines sum to the stated payment total (to the cent).
  - Each line's `source_quote` actually appears in the document. This grounding check catches hallucinated rows.
  - Every invoice is an *open* invoice for that customer (`INV-104233` and `104233` normalise to the same key), and paid + deduction doesn't exceed the open amount.
  - There are no duplicate invoices, and every deduction has a reason.
  - When the model sets `unreadable`, the advice always goes to review.

## Run and test
```bash
pip install -r requirements.txt
pytest -q        # 9 tests with a fake client: request shape, PDF encoding, stop reasons, validation rules, routing
AZURE_STORAGE_ACCOUNT_URL=https://acct.blob.core.windows.net python -m remitextract
```
