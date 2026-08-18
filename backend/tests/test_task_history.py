"""Deterministic PostgreSQL coverage for the task history read projection."""

import sys
from datetime import date
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import domain, sql, undo
from app.db import pool
from app.main import app
from app.models import CreateTaskArgs, DeleteTasksArgs, UpdateTaskArgs


ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_ACTOR_ID = UUID("00000000-0000-0000-0000-000000000002")
MISSING_TASK_ID = UUID("00000000-0000-0000-0000-0000000000ff")
SEEDED_TASK_ID = UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
def db():
    with pool.connection() as conn:
        conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
        conn.commit()
        try:
            yield conn
        finally:
            conn.execute(sql.TRUNCATE_ALL_TEST_STATE)
            conn.commit()


def _run(conn, actor_id=ACTOR_ID):
    row = conn.execute(
        sql.INSERT_RUN,
        {
            "actor_id": actor_id,
            "prompt": "task history fixture",
            "model": "task-history-fixture",
        },
    ).fetchone()
    conn.commit()
    return row["id"]


def _commit(conn, run_id, mutation, actor_id=ACTOR_ID):
    domain.write_events(run_id, actor_id, mutation.events, conn=conn)
    conn.commit()
    return mutation


def _create(conn, run_id, title="History task", **fields):
    return _commit(
        conn,
        run_id,
        domain.create_task(
            ACTOR_ID, CreateTaskArgs(title=title, **fields), conn=conn
        ),
    ).tasks[0]


def _history(task_id, **params):
    return TestClient(app).get(f"/api/tasks/{task_id}/history", params=params)


def _changes(entry):
    return {item["field"]: item for item in entry["changes"]}


def test_projection_create_update_null_and_wire_contract(db):
    run_id = _run(db)
    task = _create(
        db,
        run_id,
        notes="first note",
        due_date=date(2026, 8, 20),
        priority="high",
    )
    updated = _commit(
        db,
        run_id,
        domain.update_task(
            ACTOR_ID,
            UpdateTaskArgs(
                task_id=task.id,
                expected_version=1,
                notes="first note\nsecond note",
                due_date=None,
            ),
            conn=db,
        ),
    ).tasks[0]
    assert updated.version == 2

    response = _history(task.id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["exists_now"] is True
    assert body["current_version"] == 2
    assert body["next_before_event_id"] is None
    newest, created = body["entries"]

    assert newest["operation"] == newest["effect"] == "updated"
    assert (newest["version_before"], newest["version_after"]) == (1, 2)
    changes = _changes(newest)
    assert set(changes) == {"notes", "due_date"}
    assert changes["notes"]["before"] == "first note"
    assert changes["notes"]["after"] == "first note\nsecond note"
    assert changes["due_date"]["before"] == "2026-08-20"
    assert changes["due_date"]["after"] is None

    assert created["operation"] == created["effect"] == "created"
    assert created["version_before"] is None
    assert created["version_after"] == 1
    assert created["changes"] == []

    # Purpose-built browser projection: no raw snapshots or authority ids.
    assert set(newest) == {
        "event_id",
        "operation",
        "effect",
        "occurred_at",
        "version_before",
        "version_after",
        "snapshot",
        "changes",
    }
    assert newest["snapshot"] is None
    assert set(created["snapshot"]) == {
        "title",
        "notes",
        "due_date",
        "priority",
        "status",
        "blocked_by",
    }


def test_delete_restore_and_restored_update_shapes(db):
    setup_run = _run(db)
    delete_run = _run(db)
    deleted_task = _create(db, setup_run, title="Return me")
    _commit(
        db,
        delete_run,
        domain.delete_tasks(
            ACTOR_ID, DeleteTasksArgs(task_ids=[deleted_task.id]), conn=db
        ),
    )

    deleted = _history(deleted_task.id)
    assert deleted.status_code == 200, deleted.text
    deleted_body = deleted.json()
    assert deleted_body["exists_now"] is False
    assert deleted_body["current_version"] is None
    delete_entry = deleted_body["entries"][0]
    assert delete_entry["operation"] == delete_entry["effect"] == "deleted"
    assert (delete_entry["version_before"], delete_entry["version_after"]) == (1, None)

    # Even a cursor older than the oldest event remains authorized by audit scope.
    exhausted = _history(
        deleted_task.id,
        before_event_id=deleted_body["entries"][-1]["event_id"],
    )
    assert exhausted.status_code == 200, exhausted.text
    assert exhausted.json()["entries"] == []

    result = undo.undo_run(delete_run, ACTOR_ID)
    assert result.refused is False and result.applied == 1
    restored = _history(deleted_task.id).json()
    restore_entry = restored["entries"][0]
    assert restored["current_version"] == 2
    assert restore_entry["operation"] == "restored"
    assert restore_entry["effect"] == "created"
    assert (restore_entry["version_before"], restore_entry["version_after"]) == (None, 2)

    update_run = _run(db)
    updated_task = _create(db, setup_run, title="Original")
    _commit(
        db,
        update_run,
        domain.update_task(
            ACTOR_ID,
            UpdateTaskArgs(
                task_id=updated_task.id,
                expected_version=1,
                title="Edited",
            ),
            conn=db,
        ),
    )
    result = undo.undo_run(update_run, ACTOR_ID)
    assert result.refused is False and result.applied == 1

    compensation = _history(updated_task.id).json()["entries"][0]
    assert compensation["operation"] == "restored"
    assert compensation["effect"] == "updated"
    assert (compensation["version_before"], compensation["version_after"]) == (2, 3)
    assert _changes(compensation)["title"] == {
        "field": "title",
        "before": "Edited",
        "after": "Original",
    }


def test_seed_scope_and_blocker_cascade(db):
    db.execute(
        sql.INSERT_SEED_TASK,
        {
            "id": SEEDED_TASK_ID,
            "owner_id": ACTOR_ID,
            "title": "Seeded task",
            "notes": "",
            "due_date": None,
            "priority": "medium",
            "blocked_by": None,
        },
    )
    foreign = db.execute(
        sql.INSERT_TASK,
        {
            "owner_id": OTHER_ACTOR_ID,
            "title": "Foreign task",
            "notes": "",
            "due_date": None,
            "priority": "medium",
            "blocked_by": None,
        },
    ).fetchone()
    db.commit()

    seeded = _history(SEEDED_TASK_ID)
    assert seeded.status_code == 200, seeded.text
    assert seeded.json() == {
        "task_id": str(SEEDED_TASK_ID),
        "exists_now": True,
        "current_version": 1,
        "entries": [],
        "next_before_event_id": None,
    }

    foreign_response = _history(foreign["id"])
    missing_response = _history(MISSING_TASK_ID)
    assert foreign_response.status_code == missing_response.status_code == 403
    assert foreign_response.json() == missing_response.json()
    assert foreign_response.json()["error"]["code"] == "OUT_OF_SCOPE"

    setup_run = _run(db)
    delete_run = _run(db)
    blocker = _create(db, setup_run, title="Blocker")
    dependent = _create(
        db, setup_run, title="Dependent", blocked_by=blocker.id
    )
    _commit(
        db,
        delete_run,
        domain.delete_tasks(
            ACTOR_ID, DeleteTasksArgs(task_ids=[blocker.id]), conn=db
        ),
    )

    cascade = _history(dependent.id).json()["entries"][0]
    assert cascade["operation"] == cascade["effect"] == "updated"
    # ON DELETE SET NULL changes state without manufacturing a new version.
    assert (cascade["version_before"], cascade["version_after"]) == (1, 1)
    assert _changes(cascade)["blocked_by"]["before"] == str(blocker.id)
    assert _changes(cascade)["blocked_by"]["after"] is None


def test_cursor_pagination_is_stable_and_bounded(db):
    run_id = _run(db)
    current = _create(db, run_id, title="v1")

    for version in range(2, 6):
        current = _commit(
            db,
            run_id,
            domain.update_task(
                ACTOR_ID,
                UpdateTaskArgs(
                    task_id=current.id,
                    expected_version=current.version,
                    title=f"v{version}",
                ),
                conn=db,
            ),
        ).tasks[0]

    pages = []
    cursor = None
    for expected_versions in ([5, 4], [3, 2], [1]):
        params = {"limit": 2}
        if cursor is not None:
            params["before_event_id"] = cursor
        response = _history(current.id, **params)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body)
        assert [entry["version_after"] for entry in body["entries"]] == expected_versions
        cursor = body["next_before_event_id"]

    assert cursor is None
    ids = [
        entry["event_id"]
        for page in pages
        for entry in page["entries"]
    ]
    assert len(ids) == len(set(ids)) == 5

    invalid = _history(current.id, limit=51)
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
