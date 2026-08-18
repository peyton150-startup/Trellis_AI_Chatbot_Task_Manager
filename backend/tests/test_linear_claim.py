from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from psycopg.types.json import Json

from app import linear_agent, sql
from app.db import pool


ORG_ID = "org-t00w-claim-tests"


@pytest.fixture(autouse=True)
def clean_claim_rows():
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM linear_agent_inbox WHERE organization_id = %s",
            (ORG_ID,),
        )
        conn.commit()

    yield

    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM linear_agent_inbox WHERE organization_id = %s",
            (ORG_ID,),
        )
        conn.commit()


def insert_row(
    session_id: str,
    *,
    received_at: datetime,
    not_before: datetime | None = None,
    claimed_until: datetime | None = None,
    attempt_count: int = 0,
) -> dict:
    delivery_id = str(uuid4())
    body_sha256 = uuid4().hex + uuid4().hex

    with pool.connection() as conn:
        row = conn.execute(
            """
            INSERT INTO linear_agent_inbox (
                delivery_id,
                body_sha256,
                organization_id,
                agent_session_id,
                action,
                payload,
                status,
                attempt_count,
                claimed_until,
                not_before,
                received_at
            )
            VALUES (
                %(delivery_id)s,
                %(body_sha256)s,
                %(organization_id)s,
                %(agent_session_id)s,
                'prompted',
                %(payload)s,
                'pending',
                %(attempt_count)s,
                %(claimed_until)s,
                %(not_before)s,
                %(received_at)s
            )
            RETURNING *;
            """,
            {
                "delivery_id": delivery_id,
                "body_sha256": body_sha256,
                "organization_id": ORG_ID,
                "agent_session_id": session_id,
                "payload": Json({"test": True}),
                "attempt_count": attempt_count,
                "claimed_until": claimed_until,
                "not_before": not_before or received_at,
                "received_at": received_at,
            },
        ).fetchone()
        conn.commit()

    return dict(row)


def test_claims_oldest_pending_row_and_increments_attempt():
    now = datetime.now(timezone.utc)

    first = insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=10),
    )

    claimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)

    assert claimed is not None
    assert claimed["id"] == first["id"]
    assert claimed["attempt_count"] == 1
    assert claimed["claimed_until"] > now


def test_leased_first_row_blocks_later_row_in_same_session():
    now = datetime.now(timezone.utc)

    first = insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=10),
    )

    claimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)
    assert claimed is not None
    assert claimed["id"] == first["id"]

    assert linear_agent.claim_next_linear_inbox(lease_seconds=30) is None


def test_backing_off_first_row_blocks_due_later_row():
    now = datetime.now(timezone.utc)

    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
        not_before=now + timedelta(minutes=5),
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=10),
        not_before=now - timedelta(seconds=1),
    )

    assert linear_agent.claim_next_linear_inbox(lease_seconds=30) is None


def test_expired_lease_reclaims_first_row_before_second():
    now = datetime.now(timezone.utc)

    first = insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
        claimed_until=now - timedelta(seconds=1),
        attempt_count=1,
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=10),
    )

    claimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)

    assert claimed is not None
    assert claimed["id"] == first["id"]
    assert claimed["attempt_count"] == 2


def test_blocked_session_does_not_block_another_session():
    now = datetime.now(timezone.utc)

    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=30),
        not_before=now + timedelta(minutes=5),
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
    )

    other = insert_row(
        "session-b",
        received_at=now - timedelta(seconds=10),
    )

    claimed = linear_agent.claim_next_linear_inbox(lease_seconds=30)

    assert claimed is not None
    assert claimed["id"] == other["id"]


def test_locked_first_row_cannot_be_overtaken_within_one_session():
    """Hold worker A's claim transaction open while worker B dequeues."""
    now = datetime.now(timezone.utc)

    first = insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=10),
    )

    with pool.connection() as first_conn:
        claimed = first_conn.execute(
            sql.CLAIM_LINEAR_INBOX,
            {"lease_seconds": 30},
        ).fetchone()

        assert claimed is not None
        assert claimed["id"] == first["id"]

        # first_conn has NOT committed. Its row lock is still held.
        # Worker B must SKIP that lock without claiming prompt N+1.
        with ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(
                linear_agent.claim_next_linear_inbox,
                lease_seconds=30,
            ).result(timeout=5)

        assert second is None

        first_conn.rollback()


def test_locked_session_does_not_block_another_session():
    """SKIP LOCKED preserves concurrency across independent AgentSessions."""
    now = datetime.now(timezone.utc)

    first = insert_row(
        "session-a",
        received_at=now - timedelta(seconds=30),
    )
    insert_row(
        "session-a",
        received_at=now - timedelta(seconds=20),
    )
    other = insert_row(
        "session-b",
        received_at=now - timedelta(seconds=10),
    )

    with pool.connection() as first_conn:
        claimed = first_conn.execute(
            sql.CLAIM_LINEAR_INBOX,
            {"lease_seconds": 30},
        ).fetchone()

        assert claimed is not None
        assert claimed["id"] == first["id"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            second = executor.submit(
                linear_agent.claim_next_linear_inbox,
                lease_seconds=30,
            ).result(timeout=5)

        assert second is not None
        assert second["id"] == other["id"]

        first_conn.rollback()


@pytest.mark.parametrize("bad_lease", [0, -1])
def test_claim_rejects_nonpositive_lease(bad_lease):
    with pytest.raises(ValueError):
        linear_agent.claim_next_linear_inbox(lease_seconds=bad_lease)


def test_claim_rejects_boolean_lease():
    with pytest.raises(TypeError):
        linear_agent.claim_next_linear_inbox(lease_seconds=True)
