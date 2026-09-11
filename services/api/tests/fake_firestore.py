"""An in-process stand-in for `google.cloud.firestore.AsyncClient` — TEST ONLY.

It implements exactly the surface `store/firestore.py` uses, so the real
`FirestoreStore` code runs unchanged over it. It is not Firestore and proves
nothing about the network, IAM or indexes; the live conformance run
(`MCPFORGE_TEST_FIRESTORE=1`) does that.

What it does copy from Firestore, so a lossy payload fails here rather than in
production:

- Every write is serialized and every read returns a fresh copy.
- A value must be null, bool, int (64-bit), float, str, list or map.
- A list may not directly contain a list. Firestore rejects nested arrays.
- Map keys must be non-empty strings.
- A document may not exceed 1 MiB. The size is approximated as the UTF-8 length
  of its JSON, which differs from Firestore's own accounting by a few bytes per
  field.
- Queries stream in document-id order, which is Firestore's default.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

MAX_DOCUMENT_BYTES = 1_048_576
_INT64 = (-(2**63), 2**63 - 1)


class FirestoreValueError(ValueError):
    """A value real Firestore would refuse to store."""


def _check(value: Any, where: str, *, in_list: bool = False) -> None:
    if value is None or isinstance(value, bool | float | str):
        return
    if isinstance(value, int):
        if not _INT64[0] <= value <= _INT64[1]:
            raise FirestoreValueError(f"{where}: integer out of 64-bit range")
        return
    if isinstance(value, list):
        if in_list:
            raise FirestoreValueError(f"{where}: an array may not directly contain an array")
        for i, item in enumerate(value):
            _check(item, f"{where}[{i}]", in_list=True)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise FirestoreValueError(f"{where}: map keys must be non-empty strings")
            _check(item, f"{where}.{key}")
        return
    raise FirestoreValueError(f"{where}: unsupported type {type(value).__name__}")


class _Snapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None) -> None:
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data)


class _Document:
    def __init__(self, db: FakeFirestore, collection: str, doc_id: str) -> None:
        self._db, self._collection, self._id = db, collection, doc_id

    async def get(self) -> _Snapshot:
        await self._db.before_read(self._collection, self._id)
        return _Snapshot(self._id, self._db.data.get(self._collection, {}).get(self._id))

    async def set(self, data: dict[str, Any]) -> None:
        _check(data, f"{self._collection}/{self._id}")
        encoded = json.dumps(data).encode()
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise FirestoreValueError(
                f"{self._collection}/{self._id}: document is {len(encoded)} bytes; "
                f"Firestore's limit is {MAX_DOCUMENT_BYTES}"
            )
        self._db.writes += 1
        self._db.data.setdefault(self._collection, {})[self._id] = json.loads(encoded)


class _Query:
    def __init__(self, db: FakeFirestore, collection: str, filters: tuple[Any, ...] = ()) -> None:
        self._db, self._collection, self._filters = db, collection, filters

    def where(self, *, filter: Any) -> _Query:  # noqa: A002 - mirrors the SDK signature
        if filter.op_string != "==":
            raise NotImplementedError(f"fake supports only ==, not {filter.op_string}")
        return _Query(self._db, self._collection, (*self._filters, filter))

    async def stream(self) -> AsyncIterator[_Snapshot]:
        docs = self._db.data.get(self._collection, {})
        for doc_id in sorted(docs):
            data = docs[doc_id]
            if all(data.get(f.field_path) == f.value for f in self._filters):
                yield _Snapshot(doc_id, data)


class _Collection(_Query):
    def document(self, doc_id: str) -> _Document:
        return _Document(self._db, self._collection, doc_id)


class FakeFirestore:
    """One shared "database". Two stores over one instance model a restart."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.reads: list[tuple[str, str]] = []
        self.writes = 0

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    async def before_read(self, collection: str, doc_id: str) -> None:
        """Override to make reads fail or stall."""
        self.reads.append((collection, doc_id))
