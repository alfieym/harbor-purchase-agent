"""S14: the AG-UI HTTP surface — start, stream, replay, cancel, snapshot."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from harbor.features.runs.router import router
from harbor.features.runs.service import RunManager
from harbor.features.runs.store import SqliteRunStore

BODY = {
    "threadId": "t1",
    "buyerId": "b1",
    "shoppingSessionId": "s1",
    "messages": [{"id": "m1", "role": "user", "content": "找台灯"}],
}


class FakeResponse(BaseModel):
    final_text: str
    cards: list[dict] = []
    pending_draft: dict | None = None


async def _runner(buyer_id, session_id, query, observer, on_text_delta):
    if observer is not None:
        await observer(
            [
                {
                    "id": "call-1",
                    "name": "product_search",
                    "arguments": '{"query":"台灯"}',
                    "output": "found 1",
                }
            ]
        )
    return FakeResponse(final_text="找到 1 件商品")


@pytest.fixture()
def make_client(tmp_path: Path) -> Iterator[Callable[..., tuple[TestClient, SqliteRunStore]]]:
    """Build clients whose portal outlives a request.

    A bare ``TestClient`` spins up a fresh portal per request and shuts it down
    afterwards, which would cancel the run task the POST just started. Entering
    the client keeps one event loop alive for the whole test — the same
    lifetime a real server has.
    """
    opened: list[TestClient] = []

    def factory(runner=_runner) -> tuple[TestClient, SqliteRunStore]:
        app = FastAPI()
        app.include_router(router)
        store = SqliteRunStore(tmp_path / f"runs-{len(opened)}.sqlite3")
        app.state.run_manager = RunManager(store, runner, heartbeat_seconds=0.05)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client, store

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


def _frames(
    client: TestClient, url: str, headers: dict | None = None
) -> Iterator[tuple[int, dict]]:
    """Yield ``(seq, event)`` until the stream ends; heartbeats are skipped."""
    seq: int | None = None
    with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for line in response.iter_lines():
            if line.startswith("id: "):
                seq = int(line[4:])
            elif line.startswith("data: "):
                assert seq is not None, "a data frame must carry its id"
                yield seq, json.loads(line[6:])


def _until_terminal(
    client: TestClient, url: str, headers: dict | None = None
) -> list[tuple[int, dict]]:
    frames: list[tuple[int, dict]] = []
    for seq, event in _frames(client, url, headers):
        frames.append((seq, event))
        if event["type"] in ("RUN_FINISHED", "RUN_ERROR"):
            break
    return frames


def test_start_then_stream_the_agui_sequence(make_client) -> None:
    client, store = make_client()

    started = client.post("/agui/runs", json=BODY)
    assert started.status_code == 202
    assert started.json()["threadId"] == "t1"
    run_id = started.json()["runId"]

    frames = _until_terminal(client, f"/agui/runs/{run_id}/events")

    assert [seq for seq, _ in frames] == list(range(1, len(frames) + 1))
    assert [event["type"] for _, event in frames][0] == "RUN_STARTED"
    assert [event["type"] for _, event in frames][-1] == "RUN_FINISHED"
    assert "TOOL_CALL_START" in [event["type"] for _, event in frames]
    assert store.get_run(run_id).status == "finished"


def test_last_event_id_resumes_after_the_events_already_seen(make_client) -> None:
    client, _ = make_client()
    run_id = client.post("/agui/runs", json=BODY).json()["runId"]
    _until_terminal(client, f"/agui/runs/{run_id}/events")

    resumed = _until_terminal(client, f"/agui/runs/{run_id}/events", headers={"Last-Event-ID": "3"})

    assert [seq for seq, _ in resumed] == [4, 5, 6, 7, 8, 9, 10, 11, 12]


def test_a_refresh_rebuilds_the_run_from_its_durable_events(make_client) -> None:
    client, _ = make_client()
    run_id = client.post("/agui/runs", json=BODY).json()["runId"]
    _until_terminal(client, f"/agui/runs/{run_id}/events")

    snapshot = client.get(f"/agui/runs/{run_id}").json()
    replay = _until_terminal(client, f"/agui/runs/{run_id}/events")

    assert snapshot["status"] == "finished"
    assert snapshot["lastSeq"] == len(replay)
    assert [event["type"] for _, event in replay][0] == "RUN_STARTED"
    snapshots = [event for _, event in replay if event["type"] == "STATE_SNAPSHOT"]
    assert snapshots[-1]["snapshot"]["final_text"] == "找到 1 件商品"


def test_cancel_is_an_explicit_command(make_client) -> None:
    async def slow(buyer_id, session_id, query, observer, on_text_delta):
        import asyncio

        await asyncio.sleep(10)
        raise AssertionError("a cancelled run must not answer")

    client, _ = make_client(slow)
    run_id = client.post("/agui/runs", json=BODY).json()["runId"]

    cancelled = client.post(f"/agui/runs/{run_id}/cancel")

    assert cancelled.status_code == 200
    # The command is accepted immediately; the run's own stream is how the
    # client learns the turn actually stopped.
    frames = _until_terminal(client, f"/agui/runs/{run_id}/events")
    assert frames[-1][1]["result"] == {"status": "cancelled"}
    snapshot = client.get(f"/agui/runs/{run_id}").json()
    assert snapshot["status"] == "cancelled"
    assert snapshot["lastSeq"] == frames[-1][0]


def test_an_unknown_run_is_a_404_everywhere(make_client) -> None:
    client, _ = make_client()

    assert client.get("/agui/runs/missing").status_code == 404
    assert client.post("/agui/runs/missing/cancel").status_code == 404
    with client.stream("GET", "/agui/runs/missing/events") as response:
        assert response.status_code == 404


def test_without_a_run_manager_the_routes_report_503() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.run_manager = None
    client = TestClient(app)

    assert client.post("/agui/runs", json=BODY).status_code == 503
    assert client.get("/agui/runs/anything").status_code == 503
