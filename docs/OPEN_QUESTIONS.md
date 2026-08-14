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
