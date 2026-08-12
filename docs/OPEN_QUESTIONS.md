# Open Questions

Written to by either model when the build spec is silent, ambiguous, or self-contradictory. Writing here and stopping is always correct. Guessing and continuing is never correct.

Format:

```
## Q-01  <one line question>
Task:      T##
Blocking:  yes | no
Context:   what the spec says, and where it runs out
Options:   the candidate readings, if there is more than one
```

---

## Open

_(none open)_

---

## Resolved

Kept rather than deleted, because each one is a place a later reader will
re-derive the same question. The resolution always lives in `docs/DECISIONS.md`,
which is the authority; this section only records that the question was asked
and where the answer went.

## Q-01  How can `check` perform step 5a when it is passed no `run_id` or `tool_call_id`?
Task:      T04
Blocking:  yes
Status:    RESOLVED by D-15, authorized by the user on 2026-08-12
Context:   Section 6 step 5a requires `check` to verify that the approval row
           matches the current call, raising `APPROVAL_NOT_FOUND` otherwise. The
           signature printed in section 6 and the call site printed in section
           10 pass neither value, so the step is unimplementable as written. The
           tool body holds both: section 10 says every tool takes `ctx` carrying
           `actor_id` and the application `run_id`, and `tool_call_id` is
           `ctx.tool_call_id`.
Options:   A. Add them as required keyword-only parameters, keeping all five
              positional parameters in their specified names and order.
           B. Drop step 5a, arguing the caller loaded the row with
              `SELECT_APPROVAL`, which is keyed on those two values, so the
              match holds by construction.
Resolution: A. `BUILD_SPEC.md` is not edited; D-15 records the correction the
           way D-12 corrected section 6's stated premise without rewriting it.

## Q-02  Which SQL loads task owners for step 1, and may T04 touch `sql.py` to add it?
Task:      T04
Blocking:  yes
Status:    RESOLVED by D-17, authorized by the user on 2026-08-12
Context:   Section 6 step 1 requires `check` to load `owner_id` for every id in
           `target_task_ids`. No connection is passed, CLAUDE.md requires all
           SQL to live in `backend/app/sql.py`, and section 5's statement list
           has nothing that loads owners by a set of task ids.
           `SELECT_TASKS_FOR_OWNER` cannot serve: it filters by `owner_id`, has
           no id filter, and carries a LIMIT.
Options:   A. Add one constant to `sql.py` and expand T04's file list.
           B. Inline the SQL in `policy.py`, breaking the single-catalog rule.
           C. Pass preloaded ownership into `check`, moving an authoritative
              kernel check out to every call site.
Resolution: A. `SELECT_TASK_OWNERS`, plus `TRUNCATE_ALL_STATE`, which the
           invariant fixtures need to reset state and which section 13 already
           specifies for `POST /api/demo/reset`. T09 consumes it rather than
           duplicating it.

## Q-03  Section 11 routes `test_invariants.py` to Sol, but T04's definition of done is those tests passing.
Task:      T04
Blocking:  yes
Status:    RESOLVED by D-16, authorized by the user on 2026-08-12
Context:   Section 11 routes the file as SOL WRITES, OPUS REVIEWS. Section 12
           tags the T04 kernel OPUS ONLY and defines T04's completion as six of
           those same tests passing. Sol was not reachable for T04, and the
           required T04 status check has nothing to run without the tests.
Options:   A. Hand the six tests to Sol, blocking T04 until Sol is available.
           B. A T04-only routing exception under which Opus writes them, with
              compensating evidence.
Resolution: B, scoped to T04 alone. D-16 records the exception, its cost, the
           five required compensating measures, and the intended Sol chain for
           the split kernel tasks that follow. Only six of the thirteen tests
           were written; the other seven belong to T05, T07, and T08 and are
           absent rather than skipped, so the collected count always equals the
           proven count.

## Q-04  `backend/app/__init__.py` is specified in section 3 but does not exist.
Task:      T02, observed during T04
Blocking:  no
Status:    OPEN FOR A LATER TASK, not fixed by T04
Context:   Section 3's file tree lists `app/__init__.py`. It is absent, so `app`
           resolves as a PEP 420 namespace package. Imports work, and the T02,
           T03, and T04 gates all pass, but the failure mode is quiet: a
           namespace package silently merges any other directory named `app` on
           `sys.path`, and the collection error observed during T04's red run
           reported the package location as `unknown location`.
Options:   Add the empty file under whichever task owns `backend/app/`, or
           record a deliberate decision to rely on namespace packages.
Resolution: None yet. T04's file list does not include `__init__.py` and
           creating it would be an unauthorized scope expansion, so this is
           recorded rather than fixed.
