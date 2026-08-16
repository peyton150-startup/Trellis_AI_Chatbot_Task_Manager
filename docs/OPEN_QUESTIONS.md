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

Q-17, Q-18, Q-19. Each is recorded in full below, in number order, alongside the
resolved entries rather than duplicated here. Q-13 was withdrawn on 2026-08-15
as false when written; the entry is kept.

Q-14, Q-15, and Q-16 were resolved on 2026-08-15 and moved out of this list.
Q-14 and Q-16 by D-52; Q-15 by the user removing `T00A spike build` from
master's required status checks, after which `spike/` and its CI job were
deleted in D-13's order. Q-18 and Q-19 are recorded by T12A and neither blocks
it.

Q-12 was resolved on 2026-08-15 by D-54, which ratifies the merged T10 kernel
expansion retrospectively and unpriced. Q-17 stays open on its narrower residue:
D-50 fixed the code and D-54 records the D-31 process failure, but whether gate
authorship should be separated from implementation authorship is a schedule
question neither answers.

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

## Q-05  How is `tests/test_contract.py` excluded from CI when no pytest configuration exists and no `eval` marker is registered?
Task:      T00B
Blocking:  yes, for `test_contract.py`, the pytest configuration, and the T00B
           CI gate. Not blocking for the six Linear API facts or Gate B, which
           are measurements already recorded in `docs/DECISIONS.md` and which no
           marker decision can change.
Status:    RESOLVED by D-32, agreed with the user on 2026-08-13
Context:   Section 4.6 of `docs/LINEAR_INTEGRATION.md` says `test_contract.py`
           is "marked, network-dependent, and excluded from CI on the same
           grounds as `test_evals.py` under D-09". The T00B task prompt further
           says to mark it so `pytest -m "not eval"` does not collect it, and to
           register the marker "in the pytest configuration alongside the
           existing `eval` marker".
           Both premises of that last clause are false against master at
           954bd83. There is no pytest configuration anywhere in the repository:
           no `pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`, or
           `conftest.py`. No `eval` marker is registered. BUILD_SPEC line 791
           declares that `test_evals.py` will carry `@pytest.mark.eval`, but
           that file belongs to T24 and does not exist yet.
           That leaves a contradiction rather than a gap. The T00B gate must
           prove `pytest -m "not eval"` does not collect `test_contract.py`. Any
           marker other than `eval` leaves it collected by that exact command.
           Making the command exclude it means editing the CI command BUILD_SPEC
           line 821 fixes as `pytest -m "not eval"`, which is section 11, while
           T00B's file list permits only the section 12 table row. Creating a
           pytest configuration file is likewise outside the file list. Rule 0.4
           forbids both, and rule 0.1 directs writing the question here and
           stopping rather than picking a side.
Options:   A. Mark it `@pytest.mark.eval` and `@pytest.mark.contract`, and add
              `backend/pytest.ini` registering both. Keeps the CI command
              frozen. Costs a config file outside the file list, permanently
              overloads `eval` to mean "excluded from CI" rather than
              "behavioral evaluation", and collides at T24 because
              `pytest -m eval` would then collect a network contract test.
           B. Mark it `@pytest.mark.eval` only. No new file and no BUILD_SPEC
              edit, but the same overload and the same T24 collision, and it
              records a contract test as a behavioral eval.
           C. Use a `contract` marker and change CI to
              `pytest -m "not eval and not contract"`. Clean taxonomy, but CI
              must then name every taxonomy it excludes and the command grows a
              clause per class, which is the wrong abstraction: CI does not care
              what kind of test it is, only why it cannot run.
           D. Separate taxonomy from execution constraint. `eval` and `contract`
              describe what a test is; a new `network` marker describes an
              execution requirement. Default CI becomes `pytest -m "not network"`
              and the known assignments are fixed now rather than at T24:

                  test_contract.py   @pytest.mark.contract  @pytest.mark.network
                  test_evals.py      @pytest.mark.eval      @pytest.mark.network

              `pytest -m eval` then runs the behavioral suite and
              `pytest -m contract` the provider suite, with no exclusion
              workaround in either. `network` is preferred over a name like
              `nonci` because it records a property of the test rather than
              today's CI policy, and stays true if external tests are ever run
              deliberately in a protected nightly job.
Resolution: D, agreed with the user on 2026-08-13 during the T00B session. It is
           the only option that leaves no marker overloaded and no latent T24
           failure. Recorded here rather than acted on, because implementing it
           edits BUILD_SPEC section 11, `CLAUDE.md`, `.github/workflows/ci.yml`,
           and a new pytest configuration file, none of which are in T00B's file
           list. T00B stops here and resumes after the specification carries the
           correction.
Note:      The correction is not a search and replace of the CI command. Every
           statement that currently equates `eval` with CI exclusion has to
           move. BUILD_SPEC line 791 reads "behavioral, marked
           `@pytest.mark.eval`, excluded from CI", which becomes false the
           moment `network` is what excludes; the eval section of
           `docs/ARCHITECTURE.md` needs the same check. Missing either one
           leaves a future reader concluding that marking a test `eval` keeps it
           out of CI, after which the next network test is collected by the
           default gate.

## Q-06  May the probe create more than one throwaway issue when facts need independent state?
Task:      T00B
Blocking:  yes, for finalizing T00B. Not blocking for the six facts or Gate B,
           which are already recorded in `docs/DECISIONS.md`.
Status:    RESOLVED by D-33, agreed with the user on 2026-08-13
Context:   The T00B task prompt authorizes the probe to "create and then archive
           a single throwaway issue to confirm fact 3, and it must clean up
           after itself". As written the probe creates three, one each for facts
           3, 4, and 6, and archives every one of them in a `finally`.
           The three are not a convenience. Fact 3 ends with its issue archived,
           so a shared issue would have to be unarchived before fact 4 runs,
           which contaminates the archived-exclusion check that fact 4 exists to
           make. Fact 6 must create an issue under a client-supplied id that has
           never been used, so it cannot reuse an existing one at all. Sharing
           one issue would couple the three checks and make the probe less
           trustworthy in order to satisfy the sentence.
           The probe's current behaviour is believed correct and the
           specification should move to match it, rather than the probe being
           reshaped to obey a count that was never measured.
Options:   A. Authorize a fixed larger number, for example "up to three
              throwaway issues". Rejected: three is an artifact of today's
              probe and the wording breaks again the moment a further
              state-sensitive fact is added, which is the same defect as the
              original "a single throwaway issue".
           B. State the property rather than the count:

                  Each fact that requires uncontaminated initial state creates
                  its own throwaway issue and archives it in a `finally`.
                  Pre-existing workspace issues are never modified.

              This stays correct at four, six, or ten state-sensitive facts and
              needs no further correction as the probe grows.
Resolution: B, agreed with the user on 2026-08-13. Recorded here rather than
           acted on, because T00B is already stopped at Q-05 and the correction
           belongs to the specification, not to this task. Chat approval is not
           treated as sufficient to leave the spec saying one thing while the
           probe does another.

## Q-07  T00B's ruff gate is not executable as specified: no pin, no configuration, and a non-clean baseline
Task:      T00B
Blocking:  yes, for the T00B CI gate. Not blocking for the six facts or Gate B.
Status:    RESOLVED by D-34, agreed with the user on 2026-08-13
Context:   BUILD_SPEC line 821 states that CI runs `ruff check`. The T00B gate is
           further specified to run "ruff clean", and to install from
           `backend/requirements.txt` with no inline pin list, per D-14. Three
           separate things prevent that from being executable, and all three were
           measured against `954bd83` rather than assumed.

           **1. ruff is absent from the single backend pin source.** There is no
           `ruff` entry in `backend/requirements.txt`, and no job in
           `.github/workflows/ci.yml` invokes ruff today. D-14 states that
           "Inline `pip install` pin lists in the workflow are not permitted for
           backend jobs", so ruff must come from the pin source. Note that
           D-14's stated *rationale* concerns the probe proving API facts for
           versions the application does not install, which a linter cannot
           affect; it is D-14's *mechanism* that binds here, not its reasoning.
           `backend/requirements.txt` is not in T00B's file list.

           **2. There is no repository-owned ruff configuration.** No
           `ruff.toml`, `.ruff.toml`, or `pyproject.toml` exists anywhere in the
           repository. "ruff clean" therefore has no fixed meaning: it resolves
           to whatever the installed ruff version defaults to, and ruff can fall
           back to a user-level configuration before its built-in defaults, so
           the same pinned binary can enforce different policies on different
           machines. No user-level configuration exists on the machine that took
           these measurements, confirmed by `--isolated` returning an identical
           result, so the numbers below are ruff's genuine defaults. The drift is
           demonstrated rather than hypothetical: ruff 0.16.3's default set
           includes `I`, `UP`, `BLE`, `FURB`, `RUF`, and `SIM`, well beyond the
           `E4,E7,E9,F` that ruff defaulted to for years.

           **3. master is not clean, and T00B may not make it clean.** ruff
           0.16.3 under isolated defaults reports 22 violations repository-wide
           and 19 under `backend/`. Five of the 19 belong to
           `backend/scripts/linear_probe.py`, which T00B owns. The remaining
           **14 are the master baseline**, and every one of them sits in a file
           outside T00B's file list:

               3  backend/scripts/api_probe.py       T00 and T00R
               2  backend/app/domain.py              explicitly forbidden
               2  backend/app/idempotency.py         KERNEL, explicitly forbidden
               2  backend/tests/test_invariants.py   explicitly forbidden
               1  backend/app/config.py              explicitly out of scope
               1  backend/app/policy.py              KERNEL, explicitly forbidden
               1  backend/app/db.py                  not in T00B's file list
               1  backend/app/models.py              not in T00B's file list
               1  backend/tests/test_models.py       not in T00B's file list

           By rule: `I001` 8, `UP035` 2, `BLE001` 1, `FURB157` 1, `RUF059` 1,
           `SIM102` 1. A gate requiring ruff clean over a surface T00B is
           forbidden to repair cannot be satisfied by T00B at any version pin.
Options:   A. Clean all 14 first, as a prerequisite task. Architecturally
              cleanest and rejected on schedule: it adds a task to section 12 and
              edits two KERNEL files for lint, which trips the blast-radius gate
              and requires a named schedule cut while the Linear expansion is
              already unpaid.
           B. Define an explicit, defect-oriented rule set that master already
              satisfies, and gate prospectively. Measured and viable:

                  select = ["E4", "E7", "E9", "F"]   ->  exit 0

              clean both with and without `linear_probe.py`. That set is
              pyflakes (undefined names, unused imports and variables,
              redefinitions) plus bare `except:`, `== None`, type comparisons,
              import placement, and syntax errors. It is the real-defect tier
              rather than the style tier, and it is the set ruff itself shipped
              as its default for years. It must be documented as a deliberate
              contract validated by master passing it, not as whatever made CI
              green; had the set needed contorting to fit master, option C would
              be the honest answer instead.
              Naive alternatives are worse, not better, and were measured:
              `select = ["E","F"]` yields 51 `E501` line-too-long violations
              because ruff's default selects only `E4,E7,E9` and thereby omits
              `E501`; `["E","F","I"]` yields 59.
           C. Scope T00B's gate to T00B-owned files only, proving that T00B
              introduced no violations while leaving repository debt explicit.
              Smallest change, but it requires correcting any BUILD_SPEC language
              claiming the backend is globally ruff clean, and it gives no
              repository-wide gate going forward.
Resolution: B, agreed with the user on 2026-08-13 after the measurements above.
           The correction must settle three things together, because any one
           alone leaves the gate ambiguous:

               tool       ruff==0.16.3 in backend/requirements.txt.
                          No requirements-dev.txt: this repository deliberately
                          established one backend pin source and adding a second
                          reopens D-14 for no benefit here.
               policy     A repository-owned ruff configuration pinning
                          select = ["E4", "E7", "E9", "F"].
               surface    The exact invocation and working directory, not a bare
                          `ruff check`. ruff resolves configuration by directory
                          hierarchy, so a `backend/ruff.toml` governs files under
                          `backend/` and nothing else. Fixing the surface also
                          keeps the disposable `spike/` tree, which holds 3 of
                          the 22 repository-wide violations and is deleted before
                          T12A, from ever blocking the gate.

           Consequences to state explicitly in the correction. The 14 baseline
           violations are deferred lint-adoption debt, not violations of the
           contract being defined, and they belong to a named future task rather
           than to silence. Findings from ruff's broader default set are outside
           the contract and must not become an unwritten second gate; T00B will
           voluntarily clear the five in `linear_probe.py` as hygiene, and that
           choice must not be recorded as a requirement.

           Correction, 2026-08-13, from the T00B review. The voluntary cleanup
           above was committed to here and then not done, so this paragraph and
           the shipped code disagreed. The commitment is withdrawn rather than
           honoured, and it is struck here rather than deleted so the record
           shows it was made and why it was dropped. Clearing the five would
           rebuild exactly the second gate this question rejects, and three of
           them are percent-formatting inside GraphQL query strings, where
           f-strings would force escaping every brace in a language made of
           braces. The pinned `E4, E7, E9, F` contract is the gate, and
           "passes ruff's current defaults" is not a second one. UP035, a
           deprecated import, is the one case worth fixing on its own merits
           whenever `linear_probe.py` is next open. BUILD_SPEC line 821
           currently implies a repository-wide `ruff check` and needs the same
           semantic sweep Q-05 requires, or the documented surface and the real
           surface disagree again.
Note:      Because nothing in option B touches `policy.py` or `idempotency.py`,
           the lint work no longer trips the blast-radius gate and needs no
           re-plan or schedule cut. That is a consequence of the measurement, not
           an assumption; option A would have required both.

## Q-08  BUILD_SPEC section 12 and LINEAR_INTEGRATION section 8 disagree about what T07 is
Task:      T07
Blocking:  yes
Status:    RESOLVED by D-37, agreed with the user on 2026-08-13
Context:   At the time of Q-08, `CLAUDE.md` named both documents as sources of
           truth. Section 12 of `docs/BUILD_SPEC.md` described T07 as `undo.py`
           "as specified" and listed no T00L row. Section 8 of
           `docs/LINEAR_INTEGRATION.md` sequenced T00L before T07 and gave T07's
           done-when as "As specified, plus the diverged refusal", meaning the
           `EXTERNALLY_MODIFIED` precheck from its section 4.4. Section 12 had
           absorbed the T00B row when T00B landed and nothing else, so the two
           tables were out of step.
           This is a contradiction rather than a gap, which is the case rule 0.1
           exists for. D-36, merged while T07 was being scoped, compounds it: it
           states that "T00L and T07 add EXTERNALLY_MODIFIED logic to KERNEL
           undo.py" and commits to the delta receiving focused review at the
           post-T08 checkpoint.
Options:   A. BUILD_SPEC governs. T07 implements section 8 as written and the
              divergence clause is a later retrofit. Cheapest path to T08 and
              the ugly demo bar, and it costs a second edit to a merged KERNEL
              file if T00L is taken, which D-31 prices at a re-plan.
           B. Run T00L first, then write `undo.py` once with the clause.
              Avoids the second kernel edit and costs about half a day before
              T08 starts, on a task that is itself OPUS ONLY.
           C. Cut the Linear expansion, making the contradiction moot.
Resolution: A, agreed with the user on 2026-08-13. Recorded as D-37, which also
           demotes the LINEAR_INTEGRATION section 8 sequencing to proposed and
           re-aims D-36's review commitment at something that exists. D-46 later
           ratifies the final schedule: T00B stays after T06, while T00L and T26
           through T29 run after T25. Section 8 now matches BUILD_SPEC, so the
           original contradiction is no longer present.

## Q-09  Section 8's precheck is unimplementable for a run that touched one task twice
Task:      T07
Blocking:  yes
Status:    RESOLVED by D-38, agreed with the user on 2026-08-13
Context:   Section 8 step 3 compares each event against current database state:
           `current.version == event.after["version"]`, and for a `deleted`
           event, that the row is still absent. Both conditions are written
           against the database, and only the newest event on a task can
           satisfy them.
           A run that creates a task and then updates it leaves the create
           event's `after["version"]` at 1 while the row is at 2, so the undo
           refuses `VERSION_CONFLICT` on a run nothing else touched. A run that
           updates a task and then deletes it leaves the update event demanding
           a row its own delete removed, so the undo refuses `ROW_DISAPPEARED`.
           Both are runs the demo can produce.
Options:   A. Walk the events in the same reverse order the apply pass uses and
              compare each against projected state: the newest event on a task
              against the database, earlier ones against the state left by
              undoing the later ones. Identical to the literal reading for every
              run that touches each task once.
           B. Transcribe step 3 literally and record the refusal as a known
              limitation.
Resolution: A. Recorded as D-38, with the two-projection split the option
           statement above does not capture: the precheck compares historical
           versions from the snapshots while the apply pass guards on the
           physical version in the row, and those diverge as soon as the first
           compensation lands.

## Q-10  T07 cannot reach its own done-when, restore a deletion, or read a run completely, within `undo.py` alone
Task:      T07
Blocking:  yes
Status:    RESOLVED by D-39, D-40, and D-41, agreed with the user on 2026-08-13
Context:   Section 12 lists T07's files as `undo.py`. Four things are outside
           that list and none is optional.
           Section 11 routes `test_invariants.py` to Sol while T07's done-when
           is `test_stale_undo_refused` passing, the same collision D-16 and
           D-20 resolved for T04 and T05, each scoped to its own task.
           Section 5 has no statement that restores a deleted task under its
           original id, and `INSERT_TASK` accepts neither `id` nor `version`.
           Section 5's delete carries no version predicate, which is safe on the
           tool path and not on the undo path, where the precheck that
           established the version is a separate pass.
           `SELECT_EVENTS_FOR_RUN` carries a `LIMIT`, and a truncated read
           produces the partial undo section 8 forbids while still reporting
           success.
Options:   A. Expand the file list with recorded decisions, as T04 and T05 did.
           B. Inline the SQL in `undo.py`, which CLAUDE.md and section 5 forbid.
           C. Leave the named test to a later task, which leaves T07 with no
              definition of done.
Resolution: A. D-39 covers the three statements and the two domain entry points,
           D-40 the test-authorship exception and why the thirteen test names do
           not grow, D-41 the orchestration and cascade-event boundary.

## Q-11  How does T09 insert deterministic baseline ids without breaking writer ownership, and what exactly is fixed?
Task:      T09
Blocking:  yes
Status:    RESOLVED by D-48, authorized by the user on 2026-08-14
Context:   Section 13 requires ids from a fixed namespace but gives no namespace
           value, calls two dates only `next week`, and does not say whether
           reinsertion creates audit history. `domain.create_task` cannot accept
           deterministic ids. A bodyless FastAPI route also accepts arbitrary
           request bytes unless T09 adds enforcement from section 9's OPUS ONLY
           block, while T09 is routed to Sol.
Options:   A. Add an unaudited administrative entry point to `domain.py`.
           B. Make `seed.py` the sole reset-only writer exception, add a narrow
              `INSERT_SEED_TASK`, freeze the namespace and dates, create no
              history, and grant Sol the exact body-guard exception needed here.
Resolution: B. D-48 records the file expansion, minimum-authority SQL,
           namespace, dates, literal notes, zero-history baseline, transaction
           proof, and checkpoint-2 review after T12B. The Sol exception itself
           is T09-only and does not change T12A or T12B routing. D-49 later
           reassigns T11 to Sol.

## Q-12  The frozen tool order prevents replay after a committed delete
Task:      T10
Blocking:  was yes
Status:    RESOLVED by D-54, authorized by the user on 2026-08-15
Context:   BUILD_SPEC section 10 and the Opus `create_task` reference require
           every tool to run `policy.check` before `idempotency.acquire`, and
           the reference explains why: a refused call must take no lease.
           Section 7 and section 14 also require a repeated completed call to
           return its stored result without re-executing the mutation.

           Those requirements conflict for `delete_tasks`. A successful first
           call removes the target row. On the identical second call,
           `policy.check` performs its mandatory scope load, observes that the
           target is missing, and raises the same `OUT_OF_SCOPE` used for a
           foreign row. Execution never reaches `idempotency.acquire`, so the
           completed lease cannot return its stored result.

           This was reproduced against PostgreSQL 16 with the fixed Task A
           fixture. The owned-row lookup returned Task A with the configured
           actor before execution. The approved first call committed the
           deletion and its cascade event. The identical second call raised
           `OUT_OF_SCOPE` at `policy.py` step 1 before reading the completed
           lease. The same probe passes for Opus's `create_task`, because that
           tool has no target ids, and for updates, because their target rows
           survive the mutation.
Options:   A. Add an Opus-owned, read-only completed-replay preflight before
              policy. It returns only a completed, same-hash result and never
              creates or reacquires a lease. This preserves the rule that a
              refused new call takes no lease, but it edits the idempotency
              kernel and the frozen order, so D-31 requires a re-plan and a
              recorded decision.
           B. Permit `delete_tasks` to catch `OUT_OF_SCOPE` and inspect the
              lease in `tools.py`. This keeps the ordinary order but duplicates
              kernel lease interpretation outside `idempotency.py` and makes
              policy refusal conditional on persistence internals. Rejected as
              an unrecorded workaround.
           C. Reorder acquire ahead of policy for every tool. This makes replay
              reachable but causes a refused new call to insert a pending
              lease, directly violating the reference implementation and the
              stated reason for its order.
           D. Declare committed delete replay unsupported. This preserves the
              printed order but weakens the task-boundary resumability and
              duplicate-call claims without a recorded architecture decision.
Resolution: A, ratified retrospectively by D-54 on 2026-08-15. Option A was the
           narrowest design that preserves both security and replay semantics,
           and `idempotency.replay_completed` shipped with T10 implementing it.
           The authorization this entry required did not precede the code, which
           is the D-31 process failure D-54 records. The ratification is
           unpriced and changes no schedule number, because the work was
           absorbed inside activity F and creates no prospective demand. It is
           explicitly not precedent for editing a KERNEL file before the
           re-plan D-31 requires.

## Q-13  WITHDRAWN. T12A cannot start because T11 never ran
Task:      T12A
Blocking:  no
Status:    WITHDRAWN on 2026-08-15. The question was false when written.
Context:   The claim was that `backend/app/prompts.py` did not exist. T11 had in
           fact merged as PR #23, commit `12d229f`, with its own `T11 prompts` CI
           gate, and `origin/master` had already fast-forwarded to `cc1970f`
           carrying it at 17:03:52, ahead of the 17:22 observation. The session
           read local `master` at `a1c5aa7` and never fetched, so the checkout was
           two commits behind and the file was present upstream the whole time.
           Kept rather than deleted because the failure is worth naming: a task
           precondition was reported from a stale local ref. One `git fetch`
           before asserting that a file does not exist would have prevented it.
Resolution: None required. T11 is complete and T12A's ordering precondition under
           rule 0.3 is satisfied.

## Q-17  D-12 requires three things that cannot all hold
Task:      T10, discovered during the T12A blocked report
Blocking:  no for the code, which D-50 fixes. Yes for the process question.
Status:    OPEN, recorded on 2026-08-15
Context:   D-12 states that step 0 is an immediate classify-and-raise, that
           `policy.py` needs no change, and that "actor scope is resolved before
           the raise, never after". The owner load was private, and
           `policy.check` cannot serve as the pre-raise call: on a conditional
           call with no approval row it raises the `APPROVAL_REQUIRED`
           `PolicyError`, not the framework's `ApprovalRequired`. D-12 asserted a
           property and supplied no mechanism for it.

           D-50 resolves the code by publishing `policy.resolve_scope`, which is
           the `policy.py` change D-12 said was unnecessary. What remains open is
           the process failure. Rule 0.1 and section 1A both say a contradiction
           in the specification is written here and stopped on, never resolved by
           picking a side. T10 picked, and the resulting behavior shipped through
           a green gate that could not see it.
Options:   A. Amend D-12 to name the mechanism, leaving its ordering requirement
              intact. D-50 already implements this reading.
           B. Supersede D-12's identical-body requirement outright, so the
              ordering obligation attaches only to tools that can defer, and
              BUILD_SPEC section 10's "identical five-step body" is corrected to
              match rather than carrying a two-tool exception.
           C. Treat this as a review-process finding as well: the T10 gate was
              authored alongside the code it tests, and the two cases that would
              have caught the defect were the two the gate parameterized around.
              Whether gate authorship should be separated from implementation
              authorship is a schedule question, not a code one.

## Q-14  Four of T12A's six proofs name a client that no task has built yet
Task:      T12A
Blocking:  was yes
Status:    RESOLVED on 2026-08-15 by D-52, as option A. T12A proves the server
           side of proofs 1, 3, 4, and 5 and writes no frontend code. Gate A
           already proved the browser half in a real browser, and T13, T14, and
           T15 prove the rendered half against real components.
Context:   T12A's proof list requires that assistant-ui sends the message (1),
           that the client renders the streamed AG-UI events (3), that tool
           completion reaches the client (4), and that the board refetches and
           displays committed state (5). The verification adds "the board
           reflects committed state after refetch".

           No frontend exists. `git ls-files` matches zero paths under
           `frontend/`, there is no root `package.json`, and section 3's frontend
           tree is unbuilt. `Board.tsx` and `useBoard.ts` belong to T13 and
           `Chat.tsx` to T14, and T12A's file list is `agent.py` and `main.py`.
           Satisfying those four proofs literally means writing files two later
           tasks own, which rule 0.4 forbids.

           The server-side half of each proof is reachable inside the file list:
           an HTTP client can post `RunAgentInput` and assert the SSE event
           sequence, including `TOOL_CALL_RESULT`, and `GET /api/tasks` returns
           committed state for the refetch. Gate A already proved the browser
           half end to end in a real browser, which is what the disposable spike
           existed for, and the T12A preamble says this task wires that proven
           shape into the real agent, run record, and trust boundary.
Options:   A. Read proofs 1, 3, 4, and 5 as server-side proofs at T12A: assert
              what a client would receive and what a refetch would return, cite
              Gate A for the rendering half, and let T13, T14, and T15 prove the
              rendered half against the real components. No file-list expansion.
           B. Expand T12A to build a minimal client. Proves the sentences
              literally and writes T13 and T14 files early, which needs a file
              list expansion and an explicit decision.

## Q-15  Deleting `spike/` at T12A needs a branch-protection change this session cannot make
Task:      T12A
Blocking:  was yes for the T12A pull request, no for T12A implementation
Status:    RESOLVED on 2026-08-15 as option A. The user removed
           `T00A spike build` from master's required status checks, preserving
           the other 14 contexts, strict up-to-date checks, admin enforcement,
           and required conversation resolution. Only then were `spike/` and the
           `t00a-spike-build` job deleted, in that order, per D-13.

           Worth keeping for the process rather than the outcome. The removal was
           reported as done twice before it was, and an independent query caught
           it both times and refused to delete. A repository administration action
           taken on someone else's word is exactly the kind of precondition worth
           re-querying rather than trusting, because the failure is silent: the
           deletion would have produced a pull request that could not pass its own
           required check, which is the specific outcome D-13 exists to prevent.
Context:   Section 12's T00A block says to delete `spike/` before T12A, and 17
           spike files are still tracked. D-13 already fixed the order and the
           reason: `T00A spike build` is a required status check on `master` with
           admin enforcement, so a T12A pull request that deletes the tree fails
           its own required check, and branch protection cannot be edited from
           inside that pull request. The required check must come off master
           first, and only then may the pull request delete the CI job and the
           tree.
           Removing a required status check is a repository administration
           action against the shared default branch. It is not implementation,
           and this session does not take it unilaterally.
Options:   A. The user removes `T00A spike build` from master's required checks,
              preserving strict up-to-date checks, admin enforcement,
              conversation resolution, and every other required context. The
              T12A pull request then deletes the job and the tree, per D-13.
           B. `spike/` survives T12A and a later task deletes it. Contradicts
              section 12 and leaves disposable code alongside the production
              integration through R2.

## Q-16  The T12A verification needs a runtime model and a database that this environment does not have
Task:      T12A
Blocking:  was yes for verification, no for implementation
Status:    RESOLVED on 2026-08-15 by D-52, as option C, with half of it still
           outstanding. The database half is closed: Compose PostgreSQL was
           running and every deterministic proof executed against it. The
           deterministic gate drives the identical agent, prompt, six tools, and
           transport against a FunctionModel and is what CI runs, because CI
           holds no provider secret and BUILD_SPEC excludes network tests from
           the default gate. The live half is not closed and is not redefined
           away: MODEL_ID and a provider credential were still unset, so
           **live T12A verification is pending** and T12A is not claimed as
           completely verified.
Context:   The prescribed verification runs the prompt `Create a task called Test
           AG-UI` and asserts exactly one committed `create_task` mutation. That
           requires a live model call and a live database.
           Neither is present. There is no `.env`; `MODEL_ID`,
           `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and `DATABASE_URL` are all unset
           in the session environment, and `docker ps` lists no running
           PostgreSQL container. `config.py` also declares `anthropic_api_key`
           and no OpenAI field, though section 1 supports either provider and
           Pydantic AI reads the credential from the environment itself, so that
           asymmetry does not by itself block an OpenAI `MODEL_ID`.
           Gate A is not a substitute. The T00A spike ran against a `FunctionModel`
           and never called a provider, so no live model call has been made in
           this repository at any point.
Options:   A. Provide `MODEL_ID` and the matching key in a local `.env`, start the
              Compose database, and run the verification against the real
              provider. This is the proof the task asks for.
           B. Verify the transport deterministically against a `FunctionModel`
              that emits the `create_task` call, and run the live provider check
              separately. Makes T12A reproducible in CI, and it proves the wiring
              rather than the model's behavior, which the eval suite owns.
           C. Both: a deterministic gate for CI and one recorded live run as the
              observed evidence.

## Q-18  History does not carry across turns, and T17 needs it
Task:      T12A, deadline T17
Blocking:  no
Status:    OPEN, recorded on 2026-08-15
Context:   D-51 fixes one `agent_runs` record as one user turn, and section 9
           loads history from `agent_runs.message_history` by run id. Those two
           together mean turn two of a conversation starts with empty history.

           The schema has no thread column and cannot gain one without a D-31
           re-plan, and the alternative, letting a browser-chosen thread
           identifier select the application run, is what D-51 rejects. So the
           gap is a property of the frozen data model rather than of the
           transport decision.

           It does not block T12A, whose six proofs are all single turn, and it
           does not block T12B, whose continuation stays inside one application
           run by definition. It blocks the demo beat at ARCHITECTURE part 11,
           2:00: "Clear my tasks" produces a clarifying question, and the user's
           answer is a second turn that needs the first turn's context to mean
           anything.
Options:   A. Load history for a new run from the actor's most recent runs, so a
              turn inherits conversation context without any client identifier
              deciding which conversation it belongs to. Server-owned, no new
              column, and wrong the moment two conversations exist at once.
           B. Add a thread column and a thread-scoped history read. Correct, and
              a schema change that trips D-31 and needs a named cut to pay for
              it.
           C. Accept the limitation and let T17's clarification live inside one
              application run, by treating the clarifying question and its answer
              as one turn that the framework interrupts. Closest to how the
              approval interrupt already works, and it is the option that costs
              nothing if it turns out to be sufficient.
           D. Cut the clarification beat. It is a named demo moment, so this is a
              PROJECT_PLAN decision rather than an implementation one.

## Q-19  `render_task_block` has no caller and T23 cannot add one
Task:      T11, discovered at T12A, deadline T23
Blocking:  no
Status:    OPEN, recorded on 2026-08-15
Context:   BUILD_SPEC section 10 says `render_task_block` "is the only place task
           content enters a prompt" and that `DEMO_UNSAFE_PROMPT_MODE` is read in
           exactly one place, here. T11 shipped the function. Nothing calls it.

           T12A wires `SYSTEM_PROMPT` as the agent's instructions and does not
           call the renderer, because part 4's provenance rule places task content
           in a delimited data block and "never the system prompt or instruction
           position", so a dynamic instruction carrying the task block would
           violate the rule it exists to enforce. Where the block does belong is a
           question about the shape of the user turn.

           T23's file list is `prompts.py` and `seed.py`. Neither can add a caller
           in `agent.py`, so as scheduled T23 cannot switch on the injection path
           it owns. Note also that task content already reaches the model by a
           second route the renderer does not cover: `list_tasks` returns titles
           and notes as a tool result.
Options:   A. T23 gains `agent.py` and adds the call: the block rides in the data
              position when the flag is false, and in the instruction position
              when it is true, which is what the flag is documented to do.
           B. T12A wires it now, which means deciding the user-turn shape inside
              a transport task and ahead of the proof list that covers it.
           C. Decide that tool results are the only path task content takes to the
              model, and that `render_task_block` covers the demo toggle alone.
              Honest, but it narrows a rule section 10 states without qualification
              and should be a recorded decision rather than a silence.

## Q-20  The production browser has no contract for reaching FastAPI
Task:      T13
Blocking:  yes
Status:    RESOLVED on 2026-08-15 by D-61, as option A
Context:   T13 must fetch the authoritative `GET /api/tasks` endpoint and visibly
           render the seeded board, but the repository defines no production
           frontend-to-backend origin contract. There is no FastAPI CORS policy,
           Next.js rewrite or route handler, frontend API-origin variable, reverse
           proxy configuration, or fixed deployment topology. The deleted spike's
           permissive local CORS was explicitly throwaway and cannot be restored.

           A browser-relative `fetch("/api/tasks")` reaches the Next.js origin,
           not a separately started FastAPI server. A direct cross-origin fetch
           needs both a public backend origin and a backend CORS policy. Choosing
           either behavior would invent a contract forbidden by BUILD_SPEC section
           0. The T13 handoff therefore requires this question to be decided before
           frontend implementation continues.
Options:   A. Recommended: make Next.js the browser's same-origin facade. Add
              `frontend/next.config.ts` with a rewrite from `/api/:path*` to a
              server-only `TRELLIS_API_ORIGIN`, defaulting locally to
              `http://127.0.0.1:8000`. Authorize `frontend/next.config.ts` and
              `.env.example` in T13. The browser uses relative `/api/tasks`, no
              CORS policy or Next.js API route is added, and deployments can point
              the server-side rewrite at FastAPI without exposing that origin to
              browser code.
           B. Let the browser call FastAPI directly. Add a public frontend API-base
              variable and a narrowly configured FastAPI CORS policy. This expands
              T13 into backend `config.py` and KERNEL `main.py`, so it also requires
              the D-31 re-plan and a named schedule cut.
           C. Declare that an external reverse proxy must route `/api/*` to FastAPI
              and all other paths to Next.js. The frontend uses relative
              `/api/tasks`, but local visible verification remains blocked until a
              concrete proxy configuration and ownership task are specified.

## Q-21  T13 pinned incompatible direct and adapter-owned AG-UI client versions
Task:      T14
Blocking:  yes
Status:    RESOLVED on 2026-08-16 by D-62, as option A
Context:   Before D-62, a clean `npm ci` from `frontend/package-lock.json`
           installed the app's direct `@ag-ui/client@0.0.58` and installed
           `@ag-ui/client@0.0.57` beneath `@assistant-ui/react-ag-ui@0.0.54`.
           The adapter declares `^0.0.57`, which does not include 0.0.58 under
           semver rules for a `0.0.x` version. The official integration shape
           constructs `HttpAgent` from the direct package and passes it to
           `useAgUiRuntime`, but the production build rejects that exact shape:

               Type 'HttpAgent' is not assignable to type 'AbstractAgent'.
               Types have separate declarations of a private property '_debug'.

           The T14 handoff says not to update T13's pinned dependencies unless a
           verified incompatibility requires a user decision. This is that
           incompatibility. A type cast would conceal it rather than resolve it.
Options:   A. Recommended: align the direct `@ag-ui/client` pin to 0.0.57, the
              exact class version `@assistant-ui/react-ag-ui@0.0.54` consumes,
              and regenerate the lockfile. This is the smallest dependency
              change and follows the adapter's installed contract.
           B. Move `@assistant-ui/react-ag-ui` to a version whose declared client
              dependency accepts 0.0.58, after probing that version against the
              pinned React and assistant-ui packages. This expands the dependency
              change and requires a fresh compatibility check.
           C. Keep both versions and explicitly cast the direct `HttpAgent` at
              the adapter boundary. Runtime shapes may currently agree, but this
              suppresses the private-class mismatch and leaves two AG-UI client
              implementations in the production bundle.
