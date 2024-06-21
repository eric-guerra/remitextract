"""Run one pass over incoming/ (scheduled every few minutes by an Azure Container Apps job)."""
import json
import os

import anthropic

from .pipeline import AzureBlobStore, run


def open_invoices_for(customer: str):
    # Production: query the AR subledger. Kept as a seam so tests and dry runs can supply their own.
    raise NotImplementedError("wire to the AR open-items API")


if __name__ == "__main__":
    store = AzureBlobStore(os.environ["AZURE_STORAGE_ACCOUNT_URL"], os.environ.get("REMIT_CONTAINER", "remittances"))
    for r in run(anthropic.Anthropic(), store, open_invoices_for):
        print(json.dumps(r.__dict__))
