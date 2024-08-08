"""Blob-driven pipeline: incoming/ → extracted/ (auto-apply) or review/ (human), never lost.

  remittances/incoming/<customer>/<file>      dropped by the email-ingest function or SFTP
  remittances/extracted/<customer>/<file>.json validated result, picked up by the cash-app job
  remittances/review/<customer>/<file>.json    result + issues for the AR review queue
  remittances/processed/<customer>/<file>      original, moved after a result is written
"""
from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol

from .extract import ExtractionError, extract, validate


class BlobStore(Protocol):
    def list(self, prefix: str) -> list[str]: ...
    def read(self, name: str) -> bytes: ...
    def write(self, name: str, data: bytes, content_type: str) -> None: ...
    def move(self, src: str, dst: str) -> None: ...


@dataclass
class Result:
    blob: str
    outcome: str  # extracted | review | failed
    issues: list[str]


def _json(obj) -> bytes:
    return json.dumps(obj, indent=2, default=str).encode()


def process_one(client, store: BlobStore, name: str, open_invoices_for: Callable[[str], dict[str, Decimal]]) -> Result:
    parts = name.split("/", 2)  # incoming/<customer>/<file>
    customer, filename = (parts[1], parts[2]) if len(parts) == 3 else ("unknown", parts[-1])
    data = store.read(name)
    media_type = mimetypes.guess_type(filename)[0] or "text/plain"
    base = f"{customer}/{filename}"
    try:
        remit = extract(client, data, media_type)
    except ExtractionError as e:
        store.write(f"review/{base}.json", _json({"source": name, "error": str(e)}), "application/json")
        store.move(name, f"processed/{base}")
        return Result(name, "review", [str(e)])

    text = data.decode("utf-8", "replace") if media_type.startswith("text/") else None  # PDFs/images: no grounding check
    v = validate(remit, document_text=text, open_invoices=open_invoices_for(customer))
    payload = {"source": name, "customer": customer, "remittance": remit.model_dump(mode="json"),
               "lines_total": str(v.lines_total), "issues": v.issues}
    outcome = "extracted" if v.ok else "review"
    store.write(f"{outcome}/{base}.json", _json(payload), "application/json")
    store.move(name, f"processed/{base}")  # only after the result is safely written
    return Result(name, outcome, v.issues)


def run(client, store: BlobStore, open_invoices_for) -> list[Result]:
    out = []
    for name in store.list("incoming/"):
        try:
            out.append(process_one(client, store, name, open_invoices_for))
        except Exception as e:  # leave the blob in incoming/ for the next run
            out.append(Result(name, "failed", [f"{type(e).__name__}: {e}"]))
    return out


class AzureBlobStore:
    """azure-storage-blob implementation (DefaultAzureCredential / managed identity)."""

    def __init__(self, account_url: str, container: str):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        self.c = BlobServiceClient(account_url, credential=DefaultAzureCredential()).get_container_client(container)

    def list(self, prefix):
        return [b.name for b in self.c.list_blobs(name_starts_with=prefix)]

    def read(self, name):
        return self.c.download_blob(name).readall()

    def write(self, name, data, content_type):
        from azure.storage.blob import ContentSettings
        self.c.upload_blob(name, data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

    def move(self, src, dst):
        dst_client = self.c.get_blob_client(dst)
        dst_client.start_copy_from_url(self.c.get_blob_client(src).url)  # same account: server-side copy
        self.c.delete_blob(src)
