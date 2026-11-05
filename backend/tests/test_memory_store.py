"""S8: SQLite long-term memory — text and vector in one row, one transaction."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from harbor.features.memory import sqlite_store
from harbor.features.memory.sqlite_store import MemoryRecord, SqliteMemoryStore
from harbor.features.memory.vector import pack


def _store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "memory.sqlite3")


def _add(store: SqliteMemoryStore, buyer: str, text: str, values: list[float], model="m1"):
    return asyncio.run(store.add(buyer_id=buyer, text=text, vector=values, vector_model=model))


def test_add_persists_text_and_vector_in_one_row(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500 以内", [0.25, -0.5, 1.0])

    with sqlite3.connect(tmp_path / "memory.sqlite3") as conn:
        row = conn.execute(
            "SELECT text, vector, vector_model FROM memories WHERE id = ?",
            (record.id,),
        ).fetchone()

    assert row is not None
    assert row[0] == "预算 500 以内"
    assert row[1] == pack([0.25, -0.5, 1.0])  # same row, same write
    assert row[2] == "m1"


def test_add_or_get_returns_the_existing_row_for_the_same_text_key(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def body():
        first, created_first = await store.add_or_get(
            buyer_id="b1",
            text="预算 500 以内",
            text_key="预算500",
            vector=[1.0, 0.0],
            vector_model="m1",
        )
        second, created_second = await store.add_or_get(
            buyer_id="b1",
            text="第4轮：预算 500 以内",
            text_key="预算500",
            vector=[1.0, 0.0],
            vector_model="m1",
        )
        return first, created_first, second, created_second

    first, created_first, second, created_second = asyncio.run(body())
    assert (created_first, created_second) == (True, False)
    assert second.id == first.id
    assert asyncio.run(store.count(buyer_id="b1")) == 1


def test_concurrent_add_or_get_with_the_same_key_writes_one_row(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def body():
        return await asyncio.gather(
            *(
                store.add_or_get(
                    buyer_id="b1",
                    text="预算 500 以内",
                    text_key="预算500",
                    vector=[1.0, 0.0],
                    vector_model="m1",
                )
                for _ in range(10)
            )
        )

    results = asyncio.run(body())
    assert sum(1 for _, created in results if created) == 1
    assert len({record.id for record, _ in results}) == 1
    assert asyncio.run(store.count(buyer_id="b1")) == 1


def test_pre_s8_database_is_migrated_with_text_key(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, kind TEXT NOT NULL,
                scope TEXT NOT NULL, text TEXT NOT NULL, vector BLOB NOT NULL,
                vector_model TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "pref_old",
                "b1",
                "preference",
                "long_term",
                "旧记录",
                pack([1.0, 0.0]),
                "m1",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    store = SqliteMemoryStore(path)
    record, created = asyncio.run(
        store.add_or_get(
            buyer_id="b1",
            text="新记录",
            text_key="新记录",
            vector=[0.0, 1.0],
            vector_model="m1",
        )
    )
    assert created is True
    assert [item.text for item in asyncio.run(store.list(buyer_id="b1"))] == ["新记录", "旧记录"]
    assert record.text == "新记录"


def test_add_is_visible_after_reopen(tmp_path: Path) -> None:
    _add(_store(tmp_path), "b1", "只考虑母婴宠物", [1.0, 0.0])
    reopened = _store(tmp_path)
    records = asyncio.run(reopened.list(buyer_id="b1"))
    assert [record.text for record in records] == ["只考虑母婴宠物"]


def test_update_text_rewrites_text_and_vector(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500", [1.0, 0.0])
    updated = asyncio.run(
        store.update_text(
            buyer_id="b1",
            memory_id=record.id,
            text="预算 800",
            vector=[0.0, 1.0],
            vector_model="m2",
        )
    )
    assert updated is not None
    assert updated.id == record.id
    assert updated.text == "预算 800"
    assert updated.vector == [0.0, 1.0]
    assert updated.vector_model == "m2"
    assert updated.updated_at >= record.updated_at


def test_update_unknown_id_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert (
        asyncio.run(
            store.update_text(
                buyer_id="b1", memory_id="pref_missing", text="x", vector=[1.0], vector_model="m"
            )
        )
        is None
    )


def test_update_cannot_touch_another_buyers_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500", [1.0])
    assert (
        asyncio.run(
            store.update_text(
                buyer_id="b2", memory_id=record.id, text="被改", vector=[0.0], vector_model="m"
            )
        )
        is None
    )
    assert [r.text for r in asyncio.run(store.list(buyer_id="b1"))] == ["预算 500"]


def test_delete_removes_only_that_buyer_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500", [1.0])
    deleted = asyncio.run(store.delete(buyer_id="b2", memory_id=record.id))
    assert deleted is None
    deleted = asyncio.run(store.delete(buyer_id="b1", memory_id=record.id))
    assert deleted is not None and deleted.text == "预算 500"
    assert asyncio.run(store.list(buyer_id="b1")) == []


def test_list_is_scoped_to_one_buyer_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "b1", "第一条", [1.0])
    _add(store, "b2", "别人的", [1.0])
    _add(store, "b1", "第二条", [1.0])

    records = asyncio.run(store.list(buyer_id="b1"))
    assert [record.text for record in records] == ["第二条", "第一条"]


def test_list_respects_the_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(5):
        _add(store, "b1", f"偏好 {index}", [float(index)])
    assert len(asyncio.run(store.list(buyer_id="b1", limit=2))) == 2


def test_count_reports_the_buyer_total(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "b1", "a", [1.0])
    _add(store, "b1", "b", [1.0])
    _add(store, "b2", "c", [1.0])
    assert asyncio.run(store.count(buyer_id="b1")) == 2


def test_search_similar_orders_by_cosine(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "b1", "猫粮", [1.0, 0.0])
    _add(store, "b1", "狗粮", [0.0, 1.0])

    hits = asyncio.run(store.search_similar(buyer_id="b1", vector=[0.9, 0.1], limit=2))
    assert [record.text for record, _ in hits] == ["猫粮", "狗粮"]
    assert hits[0][1] == pytest.approx(0.9939, abs=1e-3)
    assert all(score > -1.0 for _, score in hits)


def test_search_similar_skips_other_vector_models(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "b1", "旧模型", [1.0, 0.0], model="old")
    _add(store, "b1", "新模型", [1.0, 0.0], model="new")

    hits = asyncio.run(store.search_similar(buyer_id="b1", vector=[1.0, 0.0], vector_model="new"))
    assert [record.text for record, _ in hits] == ["新模型"]


def test_search_similar_skips_dimension_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "b1", "三维", [1.0, 0.0, 0.0])
    hits = asyncio.run(store.search_similar(buyer_id="b1", vector=[1.0, 0.0]))
    assert hits == []


def test_concurrent_writers_do_not_lose_records(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def body() -> int:
        await asyncio.gather(
            *(
                store.add(buyer_id="b1", text=f"偏好 {i}", vector=[float(i)], vector_model="m")
                for i in range(20)
            )
        )
        return await store.count(buyer_id="b1")

    assert asyncio.run(body()) == 20


def test_failed_write_leaves_no_partial_row(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)

    def _boom(conn, record: MemoryRecord) -> None:
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(sqlite_store, "_insert_row", _boom)

    with pytest.raises(sqlite_store.MemoryStoreError):
        _add(store, "b1", "写不进去", [1.0])

    monkeypatch.undo()
    assert asyncio.run(store.list(buyer_id="b1")) == []


def test_schema_is_created_once_and_reopening_keeps_rows(tmp_path: Path) -> None:
    first = _store(tmp_path)
    _add(first, "b1", "a", [1.0])
    second = _store(tmp_path)
    _add(second, "b1", "b", [2.0])
    assert len(asyncio.run(second.list(buyer_id="b1"))) == 2


def test_get_returns_one_buyers_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500", [1.0, 0.0])

    fetched = asyncio.run(store.get(buyer_id="b1", memory_id=record.id))
    assert fetched is not None and fetched.text == "预算 500"
    assert fetched.vector == [1.0, 0.0]
    assert asyncio.run(store.get(buyer_id="b2", memory_id=record.id)) is None
    assert asyncio.run(store.get(buyer_id="b1", memory_id="pref_missing")) is None


# ---------------------------------------------------------------------------
# S8 v2: versioning, the 200-record cap, structured dislikes
# ---------------------------------------------------------------------------


def _add_dislike(store: SqliteMemoryStore, buyer: str, text: str, tags: tuple[str, ...]):
    return asyncio.run(
        store.add(
            buyer_id=buyer,
            text=text,
            vector=[1.0],
            vector_model="m1",
            kind="dislike",
            material_tags=tags,
        )
    )


def test_update_bumps_the_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500", [1.0])
    assert record.version == 1

    updated = asyncio.run(
        store.update_text(
            buyer_id="b1",
            memory_id=record.id,
            text="预算 800",
            vector=[0.0],
            vector_model="m1",
            expected_version=1,
        )
    )

    assert updated is not None and updated.version == 2


def test_stale_expected_version_raises_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _add(store, "b1", "预算 500", [1.0])
    asyncio.run(
        store.update_text(
            buyer_id="b1",
            memory_id=record.id,
            text="预算 800",
            vector=[0.0],
            vector_model="m1",
            expected_version=1,
        )
    )

    with pytest.raises(sqlite_store.MemoryVersionConflict) as caught:
        asyncio.run(
            store.update_text(
                buyer_id="b1",
                memory_id=record.id,
                text="预算 900",
                vector=[0.0],
                vector_model="m1",
                expected_version=1,
            )
        )

    assert caught.value.current.text == "预算 800"
    assert caught.value.current.version == 2
    assert asyncio.run(store.get(buyer_id="b1", memory_id=record.id)).text == "预算 800"


def test_max_count_rejects_the_record_after_the_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def body():
        for index in range(2):
            await store.add_or_get(
                buyer_id="b1",
                text=f"偏好 {index}",
                vector=[1.0],
                vector_model="m1",
                text_key=f"偏好{index}",
                max_count=2,
            )
        return await store.add_or_get(
            buyer_id="b1",
            text="偏好 2",
            vector=[1.0],
            vector_model="m1",
            text_key="偏好2",
            max_count=2,
        )

    with pytest.raises(sqlite_store.MemoryLimitReached):
        asyncio.run(body())

    assert asyncio.run(store.count(buyer_id="b1")) == 2


def test_dislikes_are_exempt_from_the_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for index in range(2):
        _add(store, "b1", f"偏好 {index}", [1.0])

    record = asyncio.run(
        store.add_or_get(
            buyer_id="b1",
            text="避开塑料",
            vector=[1.0],
            vector_model="m1",
            kind="dislike",
            material_tags=("塑料",),
            max_count=None,
        )
    )
    assert record[1] is True


def test_kind_filter_and_material_tags_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, "b1", "预算 500", [1.0])
    _add_dislike(store, "b1", "避开塑料", ("塑料", "玻璃"))

    dislikes = asyncio.run(store.list(buyer_id="b1", kind="dislike"))
    assert [record.text for record in dislikes] == ["避开塑料"]
    assert dislikes[0].material_tags == ("塑料", "玻璃")

    everything = asyncio.run(store.list(buyer_id="b1", kind=None))
    assert {record.kind for record in everything} == {"preference", "dislike"}


def test_pre_s8_database_is_migrated_with_version_and_material_tags(tmp_path: Path) -> None:
    path = tmp_path / "memory.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, kind TEXT NOT NULL,
                scope TEXT NOT NULL, text TEXT NOT NULL, vector BLOB NOT NULL,
                vector_model TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, text_key TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "pref_old",
                "b1",
                "preference",
                "long_term",
                "旧记录",
                pack([1.0]),
                "m1",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "旧记录",
            ),
        )

    store = SqliteMemoryStore(path)
    [record] = asyncio.run(store.list(buyer_id="b1"))
    assert record.version == 1
    assert record.material_tags == ()
