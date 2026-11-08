"""SQLite run log (S14): numbered before broadcast, replayable after restart."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harbor.features.runs.store import RunExists, RunNotFound, SqliteRunStore


def _store(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(tmp_path / "nested" / "runs.sqlite3")


def _create(store: SqliteRunStore, run_id: str = "r1") -> None:
    store.create_run(
        run_id=run_id,
        thread_id="t1",
        buyer_id="b1",
        shopping_session_id="s1",
        input_text="找台灯",
        # S14 semantics: a run created here is already executing in this
        # process; the queued default belongs to the S15 worker path.
        status="running",
    )


def test_create_and_read_a_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    run = store.get_run("r1")
    assert run is not None
    assert run.run_id == "r1"
    assert run.thread_id == "t1"
    assert run.status == "running"
    assert run.last_seq == 0
    assert run.error is None
    assert store.get_run("missing") is None


def test_duplicate_run_id_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    with pytest.raises(RunExists):
        _create(store)


def test_events_get_contiguous_sequence_numbers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    seqs = [store.append_event("r1", {"type": "STEP_STARTED", "n": index}) for index in range(3)]

    assert seqs == [1, 2, 3]
    assert store.get_run("r1").last_seq == 3
    assert [seq for seq, _ in store.list_events("r1")] == [1, 2, 3]


def test_sequence_numbers_survive_a_new_store_instance(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite3"
    first = SqliteRunStore(path)
    _create(first)
    first.append_event("r1", {"type": "RUN_STARTED"})

    second = SqliteRunStore(path)
    assert second.append_event("r1", {"type": "STEP_STARTED"}) == 2
    assert second.list_events("r1")[0][1] == {"type": "RUN_STARTED"}


def test_list_events_resumes_after_a_seq(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    for index in range(4):
        store.append_event("r1", {"index": index})

    resumed = store.list_events("r1", after_seq=2)
    assert [seq for seq, _ in resumed] == [3, 4]
    assert [event["index"] for _, event in resumed] == [2, 3]


def test_appends_from_many_threads_do_not_reuse_a_seq(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)

    with ThreadPoolExecutor(max_workers=8) as pool:
        seqs = list(pool.map(lambda index: store.append_event("r1", {"index": index}), range(40)))

    assert sorted(seqs) == list(range(1, 41))


def test_appending_to_an_unknown_run_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RunNotFound):
        store.append_event("missing", {"type": "RUN_STARTED"})


def test_finish_records_the_terminal_status_and_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store)
    store.finish_run("r1", status="failed", error="model_timeout")

    run = store.get_run("r1")
    assert run.status == "failed"
    assert run.error == "model_timeout"


def test_a_terminal_event_and_its_status_commit_together(tmp_path: Path) -> None:
    """A client that can see the ending must already be able to read it.

    ``append_event`` is what the manager broadcasts from, so the run's status
    has to land in the same transaction as the terminal event — otherwise a
    GET issued right after ``RUN_FINISHED`` can still answer ``running``.
    """
    store = _store(tmp_path)
    _create(store)

    seq = store.append_event("r1", {"type": "RUN_FINISHED"}, status="finished")

    run = store.get_run("r1")
    assert seq == 1
    assert run.status == "finished"
    assert run.last_seq == 1


def test_interrupt_running_marks_only_running_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _create(store, "r1")
    _create(store, "r2")
    store.finish_run("r2", status="finished")

    interrupted = store.interrupt_running()

    assert interrupted == ["r1"]
    assert store.get_run("r1").status == "interrupted"
    assert store.get_run("r2").status == "finished"
    assert store.interrupt_running() == []
