CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE task_status   AS ENUM ('open', 'done');
CREATE TYPE task_priority AS ENUM ('low', 'medium', 'high', 'critical');
CREATE TYPE run_status    AS ENUM ('running', 'awaiting_approval', 'completed', 'failed', 'interrupted');
CREATE TYPE event_op      AS ENUM ('created', 'updated', 'deleted', 'restored');
CREATE TYPE lease_status  AS ENUM ('pending', 'completed', 'failed');
CREATE TYPE approval_state AS ENUM ('pending', 'approved', 'denied');

CREATE TABLE tasks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id    uuid NOT NULL,
  title       text NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
  notes       text NOT NULL DEFAULT '',
  due_date    date,
  priority    task_priority NOT NULL DEFAULT 'medium',
  status      task_status NOT NULL DEFAULT 'open',
  blocked_by  uuid REFERENCES tasks(id) ON DELETE SET NULL,
  version     integer NOT NULL DEFAULT 1,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tasks_owner_idx ON tasks (owner_id, status, due_date);

CREATE TABLE agent_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  actor_id         uuid NOT NULL,
  prompt           text NOT NULL,
  status           run_status NOT NULL DEFAULT 'running',
  message_history  jsonb NOT NULL DEFAULT '[]'::jsonb,
  model            text NOT NULL,
  model_calls      integer NOT NULL DEFAULT 0,
  tool_calls       integer NOT NULL DEFAULT 0,
  input_tokens     integer NOT NULL DEFAULT 0,
  output_tokens    integer NOT NULL DEFAULT 0,
  cost_cents       numeric(10,4) NOT NULL DEFAULT 0,
  error            text,
  started_at       timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz
);
CREATE INDEX agent_runs_status_idx ON agent_runs (status, started_at DESC);

CREATE TABLE task_events (
  id         bigserial PRIMARY KEY,
  task_id    uuid NOT NULL,
  run_id     uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  actor_id   uuid NOT NULL,
  operation  event_op NOT NULL,
  before     jsonb,
  after      jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX task_events_run_idx  ON task_events (run_id, id);
CREATE INDEX task_events_task_idx ON task_events (task_id, id DESC);

CREATE TABLE tool_invocations (
  run_id         uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  tool_call_id   text NOT NULL,
  tool_name      text NOT NULL,
  arguments_hash text NOT NULL,
  status           lease_status NOT NULL DEFAULT 'pending',
  attempt          integer NOT NULL DEFAULT 1,
  lease_expires_at timestamptz NOT NULL,
  result         jsonb,
  error          text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  completed_at   timestamptz,
  PRIMARY KEY (run_id, tool_call_id)
);

CREATE TABLE approvals (
  run_id          uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  tool_call_id    text NOT NULL,
  tool_name       text NOT NULL,
  arguments       jsonb NOT NULL,
  arguments_hash  text NOT NULL,
  required_reason text NOT NULL CHECK (required_reason IN ('destructive','blast_radius')),
  preview         jsonb NOT NULL,
  decision        approval_state NOT NULL DEFAULT 'pending',
  expires_at      timestamptz NOT NULL,
  decided_at      timestamptz,
  PRIMARY KEY (run_id, tool_call_id)
);
