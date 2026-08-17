-- T00L. The local PostgreSQL consistency boundary for Linear projection.
--
-- This migration contains no remote Linear behavior. It creates local
-- integration bookkeeping only: a tombstoned side table, an outbox, and the
-- trigger that couples the outbox to the audit log inside one transaction.
-- The projector, the reconciler, and every GraphQL call belong to T26 through
-- T29 and are deferred.
--
-- 001_init.sql is already merged and is not edited. See D-25 and D-26.

CREATE TYPE linear_operation AS ENUM ('create', 'update', 'archive', 'unarchive');
CREATE TYPE linear_delivery  AS ENUM ('pending', 'completed', 'failed');

-- D-26. Integration state is a side table, never columns on tasks.
--
-- task_id is a bare primary key with NO FOREIGN KEY to tasks(id). That absence
-- is the design, not an oversight. ON DELETE CASCADE would destroy external_id
-- in the same transaction that queues the archive projection, leaving the
-- projector a task_id and no issue to address. The row is a tombstone and
-- outlives the task on purpose, which is also what lets a restored task rejoin
-- its original external identity under its original id.
--
-- The same absence means TRUNCATE ... CASCADE over the business tables cannot
-- reach this table. Production reset leaves it alone deliberately, and the
-- deterministic suite clears it through sql.TRUNCATE_ALL_TEST_STATE.
CREATE TABLE linear_task_state (
  task_id             uuid PRIMARY KEY,
  external_id         text,
  external_updated_at timestamptz,
  diverged            boolean NOT NULL DEFAULT false,
  last_reconciled_at  timestamptz
);

-- D-25. The outbox. event_id is the primary key rather than a surrogate, which
-- supplies the UNIQUE(event_id) delivery-deduplication guarantee for free and
-- makes the dequeue key the same as the audit ordering key.
--
-- There is no payload column. task_events.before and task_events.after are
-- immutable and authoritative, so a second copy of the same change could only
-- drift, and populating it would force field mapping into PL/pgSQL.
CREATE TABLE linear_projections (
  event_id      bigint PRIMARY KEY REFERENCES task_events(id),
  task_id       uuid NOT NULL,
  operation     linear_operation NOT NULL,
  status        linear_delivery NOT NULL DEFAULT 'pending',
  attempt_count integer NOT NULL DEFAULT 0,
  remote_id     text,
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);
CREATE INDEX linear_projections_pending_idx
  ON linear_projections (status, event_id);

-- D-25. The coupling is structural rather than remembered.
--
-- An invariant enforced by every call site remembering to enqueue is an
-- invariant that breaks under time pressure. Because this fires AFTER INSERT
-- ON task_events, a committed audit event and its projection row are written by
-- the database in the same transaction and cannot diverge, a rolled-back event
-- leaves no projection, and a write that bypassed the audit log correctly does
-- not project.
--
-- It is also why applying this migration to an existing database backfills
-- nothing: historical task_events rows were inserted before the trigger existed.
--
-- restored maps to unarchive, never to update. Undo of a delete writes a
-- restored event while the Linear issue is archived, so an update would leave
-- the issue archived while the local board shows the task back.
CREATE FUNCTION linear_enqueue_projection() RETURNS trigger AS $$
BEGIN
  INSERT INTO linear_projections (event_id, task_id, operation)
  VALUES (
    NEW.id,
    NEW.task_id,
    CASE NEW.operation
      WHEN 'created'  THEN 'create'::linear_operation
      WHEN 'updated'  THEN 'update'::linear_operation
      WHEN 'deleted'  THEN 'archive'::linear_operation
      WHEN 'restored' THEN 'unarchive'::linear_operation
    END
  );
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER task_events_linear_projection
  AFTER INSERT ON task_events
  FOR EACH ROW
  EXECUTE FUNCTION linear_enqueue_projection();
