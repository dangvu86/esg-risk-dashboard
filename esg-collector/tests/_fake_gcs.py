"""In-memory fake of the google-cloud-storage Bucket/Blob subset we use.

Honors if_generation_match semantics so lock + state logic is unit-testable
with no network. Raises google.api_core.exceptions.PreconditionFailed on a
generation mismatch, exactly like the real client.
"""
from __future__ import annotations

from google.api_core.exceptions import NotFound, PreconditionFailed


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str):
        self._bucket = bucket
        self.name = name

    # --- generation helpers -------------------------------------------------
    @property
    def generation(self):
        rec = self._bucket._store.get(self.name)
        return rec[1] if rec else None

    def exists(self):
        return self.name in self._bucket._store

    def reload(self):
        if self.name not in self._bucket._store:
            raise NotFound(self.name)

    def _check_precondition(self, if_generation_match):
        if if_generation_match is None:
            return
        current = self.generation or 0
        if int(if_generation_match) != int(current):
            raise PreconditionFailed(
                f"generation mismatch: expected {if_generation_match}, have {current}"
            )

    def _write(self, data: bytes, if_generation_match):
        self._check_precondition(if_generation_match)
        self._bucket._gen += 1
        self._bucket._store[self.name] = (data, self._bucket._gen)

    # --- upload -------------------------------------------------------------
    def upload_from_string(self, data, if_generation_match=None, **_):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._write(data, if_generation_match)

    def upload_from_filename(self, filename, if_generation_match=None, **_):
        with open(filename, "rb") as f:
            self._write(f.read(), if_generation_match)

    # --- download -----------------------------------------------------------
    def download_as_text(self):
        rec = self._bucket._store.get(self.name)
        if rec is None:
            raise NotFound(self.name)
        return rec[0].decode("utf-8")

    def download_to_filename(self, filename):
        rec = self._bucket._store.get(self.name)
        if rec is None:
            raise NotFound(self.name)
        with open(filename, "wb") as f:
            f.write(rec[0])

    # --- delete / acl -------------------------------------------------------
    def delete(self, if_generation_match=None):
        self._check_precondition(if_generation_match)
        self._bucket._store.pop(self.name, None)

    def make_public(self):
        self._bucket.public.add(self.name)


class FakeBucket:
    def __init__(self, name="esg-scan-data"):
        self.name = name
        self._store: dict[str, tuple[bytes, int]] = {}
        self._gen = 0
        self.public: set[str] = set()

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)
