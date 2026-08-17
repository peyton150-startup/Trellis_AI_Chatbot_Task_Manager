-- T00W. OAuth installation and AgentSession conversation transport. See D-69.
--
-- This migration adds the conversation plane only. It does not touch
-- linear_task_state or linear_projections, does not change the T00L trigger,
-- and adds no issue projection behavior. T26 through T29 remain deferred.
--
-- 001_init.sql and 002_linear.sql are already merged and are not edited.
-- See D-25, D-26, and D-68.

CREATE TYPE linear_install_status AS ENUM ('active', 'revoked');
CREATE TYPE linear_inbox_status   AS ENUM ('pending', 'completed', 'failed', 'refused');

-- The workspace installation and the one credential lifecycle D-69 fixes.
--
-- access_token and refresh_token are secret material. They live here rather
-- than in configuration because they rotate: Linear issues a new refresh token
-- on every refresh, so a value pinned in the environment would be stale after
-- the first rotation.
--
-- allowed_linear_user_id is the single Linear human bound to the demo actor. It
-- is stored on the installation rather than read from configuration at decision
-- time so that the authorization a webhook is judged against is the one
-- captured when the workspace was installed.
CREATE TABLE linear_installations (
  id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id         text NOT NULL,
  oauth_client_id         text NOT NULL,
  app_user_id             text NOT NULL,
  allowed_linear_user_id  text NOT NULL,
  access_token            text NOT NULL,
  refresh_token           text,
  access_token_expires_at timestamptz NOT NULL,
  granted_scopes          text NOT NULL,
  status                  linear_install_status NOT NULL DEFAULT 'active',
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now()
);

-- At most one ACTIVE installation, enforced by the database rather than by a
-- check in application code. A partial unique index over a constant is the
-- whole mechanism: two concurrent installations cannot both commit, because the
-- second violates the index rather than losing a race that a SELECT then INSERT
-- would not have seen. Revoked rows are unconstrained and accumulate as history.
CREATE UNIQUE INDEX linear_installations_single_active_idx
  ON linear_installations ((true)) WHERE status = 'active';

CREATE INDEX linear_installations_identity_idx
  ON linear_installations (organization_id, oauth_client_id, app_user_id);

-- OAuth state. Single use, expiring, and server owned.
--
-- state_hash rather than the state itself, so that a database read cannot
-- replay an authorization the operator started. The callback proves matching,
-- unexpired, and unconsumed in one guarded UPDATE, and consumed_at is what
-- makes the second attempt fail rather than a SELECT the caller could race.
CREATE TABLE linear_oauth_states (
  state_hash  text PRIMARY KEY,
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  consumed_at timestamptz
);

-- Durable ingress. A row here is committed before the webhook returns 200, so a
-- backend restart cannot lose accepted work.
--
-- Two independent identities, and D-69 requires both. delivery_id is Linear's
-- own delivery UUID and is ordinary provider retry idempotency. body_sha256 is
-- defense in depth: the HMAC authenticates the body, not the header, so an
-- attacker replaying an identical valid signed body under a fresh delivery id
-- would otherwise buy a second unit of work. Both are UNIQUE and either one
-- rejecting is a duplicate.
--
-- claimed_until is a lease, not a processing boolean. A worker that dies
-- holding a boolean strands its row forever; a lease expires and the work
-- becomes reclaimable. That mirrors tool_invocations, which made the same
-- choice for the same reason.
CREATE TABLE linear_agent_inbox (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  delivery_id       text NOT NULL UNIQUE,
  body_sha256       text NOT NULL UNIQUE,
  organization_id   text NOT NULL,
  agent_session_id  text NOT NULL,
  action            text NOT NULL,
  payload           jsonb NOT NULL,
  status            linear_inbox_status NOT NULL DEFAULT 'pending',
  attempt_count     integer NOT NULL DEFAULT 0,
  claimed_until     timestamptz,
  not_before        timestamptz NOT NULL DEFAULT now(),
  refusal_reason    text,
  last_error        text,
  run_id            uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  received_at       timestamptz NOT NULL DEFAULT now(),
  completed_at      timestamptz,

  -- A refused row records why it was refused, and a row that is not refused
  -- carries no reason. Without this a permanent refusal could be written with
  -- an empty explanation, which is the state an operator most needs to read.
  CONSTRAINT linear_agent_inbox_refusal_check
    CHECK ((status = 'refused') = (refusal_reason IS NOT NULL))
);

-- The dequeue path. Ordered by received_at so one session's events are taken in
-- arrival order, which is what makes the created-before-prompted ordering in
-- D-69 enforceable by the claim query rather than by the worker remembering.
CREATE INDEX linear_agent_inbox_claimable_idx
  ON linear_agent_inbox (status, not_before, received_at);

CREATE INDEX linear_agent_inbox_session_idx
  ON linear_agent_inbox (organization_id, agent_session_id, received_at);

-- Transport integration state, not a conversation identity.
--
-- One Linear AgentSession is NOT one agent_runs.id. D-67 stays intact: an
-- ordinary user turn is one server-issued application run, and continuity is
-- inherited from an eligible completed predecessor. This table holds only the
-- cursor naming that predecessor, so a failed turn simply leaves the cursor
-- where it was.
--
-- last_completed_run_id has no ON DELETE CASCADE because agent_runs rows are
-- never deleted; SET NULL is the honest behavior if that ever changes, leaving
-- the session present with no eligible predecessor rather than vanishing.
CREATE TABLE linear_agent_sessions (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id       text NOT NULL,
  agent_session_id      text NOT NULL,
  last_completed_run_id uuid REFERENCES agent_runs(id) ON DELETE SET NULL,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT linear_agent_sessions_identity_key
    UNIQUE (organization_id, agent_session_id)
);
