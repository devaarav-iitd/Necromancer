# Necromancer — Architecture Decisions (Codex Sessions)

This document records the architecture sessions with Codex where the core
design was stress-tested and decided. GPT-5.6 (via Codex) acted as an
engineering partner; key decisions and the human pushback that shaped them
are preserved here.

---

## Session 1 — Initial architecture stress-test

• I’ll pressure-test the failure modes first, then propose a concrete controller contract, score, and package layout. I’m also checking the current OpenAI integration
guidance so the API boundary isn’t built around stale assumptions.

• Explored
└ Read SKILL.md (openai-docs skill)

• Searching the web

• Searched the web for https://developers.openai.com/api/docs/guides/latest-model.md

• Searching the web

• Searched the web for site:developers.openai.com API Responses API structured outputs Python

• Searching the web

• Searched the web for site:developers.openai.com/api/docs/models/gpt-5.6 GPT-5.6 structured outputs Responses API

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

• The core architecture is sound, but the safety comes from making the Director fully deterministic. Treat GPT as a proposer and explainer—not the authority that
decides whether a change is safe or successful.

Your “four GPT stages” should therefore be:

1. Coroner
2. Archaeologist
3. Surgeon
4. Historian

The Director is deterministic controller code. GPT-5.6 supports the Responses API and structured outputs; use strict schemas for every model boundary. The gpt-5.6
alias currently routes to GPT-5.6 Sol. OpenAI model docs (https://developers.openai.com/api/docs/models/gpt-5.6-sol)

## Where the loop can spiral or deadlock

- False baseline: initial install or collection fails, so “0 passing” provides no meaningful target. Handle this as a distinct bootstrap/collection phase; do not
  start normal test scoring until collection succeeds once.

- Flaky tests: a candidate can appear to regress or improve by chance. Run collection twice at baseline; if node IDs differ, mark the repo unstable. For a candidate
  that loses a previously passing test, rerun just those lost node IDs once before rejecting it.

- Ping-pong fixes: the model alternates between two dependency versions or repeatedly proposes near-identical diffs. Deduplicate normalized diff hashes and
  permanently close a plan item after its original attempt and one repair attempt fail.

- Hidden tests: a patch can “improve” results by changing pytest.ini, conftest.py, test discovery, skip markers, or test files. Default policy should prohibit
  modifications to all of those.

- Failure masking: --maxfail, ignored paths, broad exception handling, or skip injection can turn failures into misleading success. Run the full suite without
  --maxfail; require the original test-node universe to still be present.

- State drift: applying/rolling back in one working tree creates leftover files, generated metadata, caches, and partial patches. Never mutate the target repo during
  the loop. Each candidate starts from a fresh copy of the last accepted snapshot; “rollback” means discard that candidate directory.

- Budget starvation: “15 iterations” plus a repair attempt can become 30 evaluations. Define the 15 limit as candidate evaluations including repairs, with a global
  monotonic deadline. Stop early after three closed plan items produce no accepted progress.

## Patch-application policy

Have Surgeon return structured JSON containing plan_id, rationale, expected affected tests, file preimage SHA-256 values, and one unified diff.

Before application, deterministically reject:

- Invalid or empty diffs; duplicate diff hashes.
- Any preimage hash mismatch.
- Binary patches, symlinks, mode changes, renames, deletions, submodules, or paths containing .. / .git.
- Changes to tests/**, test/**, conftest.py, pytest.ini, tox.ini, or pytest sections in pyproject.toml.
- More than, say, 4 files or 120 changed lines without an explicit “high-risk” plan item.
- Diffs that add pytest.skip, xfail, --ignore, --maxfail, or test-selection options.

Use git apply --check followed by application in a fresh candidate checkout. Do not allow fuzzy application. If candidate 1 fails, the repair prompt gets its exact
install/test evidence, but produces a new patch against the accepted snapshot—not a patch-on-patch repair.

Tradeoff: this will reject some legitimate test-framework migrations. For a hackathon, that is a good default: tests are the specification. Add an explicit --allow-
test-migration mode later, with prominent human-review labeling.

## Sandboxing

A venv plus subprocess timeouts is enough for trusted benchmark repos, but it is not a real sandbox. setup.py, build backends, imports, tests, and subprocesses can
still read files, use the network, fork processes, or escape the venv.

For the hackathon, I’d use:

- Per-attempt temporary working copy and venv.
- Sanitized environment: no API keys, no user HOME, no credentials, fixed PYTHONHASHSEED=0, TZ=UTC, LC_ALL=C.
- Process-group timeouts: terminate the entire group, then force-kill it.
- Per-command limits: e.g. install 45s, collection 30s, pytest 60s, all capped by the 20-minute global deadline.
- Unix resource limits for CPU time, open files, and address space where supported.
- Separate pip cache/wheelhouse and PIP_NO_INPUT=1.

For arbitrary public repositories, use Docker/Podman: network disabled during tests, read-only base filesystem, bounded writable mount, CPU/memory/PID limits, and no
mounted secrets. The important caveat is that dependency installation itself runs arbitrary package code, so “venv sandboxing” must never be described as secure
isolation.

## Test-result parsing

Choose neither regex nor pytest-json-report as the source of truth.

Use pytest’s built-in JUnit XML:

pytest -q --disable-warnings --junitxml=<attempt>/pytest.xml

Parse the XML by node ID for pass/fail/error/skip outcomes. Preserve stdout/stderr and pytest’s terminal output as diagnostic evidence for Coroner and Surgeon. Regex
is acceptable only for extracting concise error snippets for prompts, never for controller decisions.

If choosing strictly between your two options, use pytest-json-report over regex—but it adds a dependency, may conflict with old pytest versions, and makes the
harness itself another installation failure. JUnit XML has the best hackathon reliability.

Treat exit codes 2–5, missing/corrupt XML, and timeouts as infrastructure failures, not ordinary test failures.

## Exact progress rule

First, run pytest --collect-only -q twice. When it succeeds consistently, freeze its ordered set of node IDs as (U). If it cannot collect yet, use a separate
bootstrap score and do not claim test progress.

For a full test run (r):

- (P(r) = {u \in U \mid u\text{ passed}})
- (M(r) = {u \in U \mid u\text{ is missing from the report}})
- collection_complete(r) is true only with no collection errors, valid JUnit XML, and every node in (U) reported.

Define each node’s non-pass penalty:

0 = pass
0 = baseline-known skip/xfail that remains skip/xfail
2 = failure
3 = error
4 = newly skipped or newly xfailed
5 = missing from report
∞ = timeout or invalid result artifact

Then:

debt(r) = Σ penalty(u, r) for u in U
score(r) = (
1 if collection_complete(r) else 0,
|P(r)|,
-debt(r)
)

Accept a candidate only when all are true:

collection_complete(candidate)
P(best) ⊆ P(candidate)
score(candidate) > score(best)

The set-inclusion check is stronger than your stated count rule: it prevents one formerly passing test from being traded for another. The score still permits useful
movement such as error → failure, but never pass → failure or pass → skip.

Before collection is possible, maximize this bootstrap score instead:

bootstrap_score = (
1 if collection succeeds with at least one test else 0,
collected_node_count,
-collection_error_count,
-installation_error_count
)

Freeze (U) on the first successful collection. A repo with unstable collection or test IDs should finish as partial / human review, not “revived.”

## Suggested package structure

src/necromancer/
cli.py
config.py
domain/
models.py # Pydantic schemas: certificates, plans, patches, results
enums.py
llm/
client.py # Responses API, strict structured-output calls
prompts/
coroner.md
archaeologist.md
surgeon.md
historian.md
diagnosis/
coroner.py
planning/
archaeologist.py
repair/
surgeon.py
patch_policy.py # allowed paths, diff limits, anti-cheat rules
patch_apply.py
execution/
workspace.py # immutable source + accepted/candidate snapshots
environment.py # venv creation, sanitized environment
subprocesses.py # groups, limits, deadlines, captured logs
install.py
pytest_runner.py
junit.py
scoring.py
controller/
director.py # deterministic state machine
state.py # persisted run ledger
budget.py
reporting/
historian.py
templates.py

Keep run evidence outside the revived repo, e.g. an output directory containing immutable attempt logs, XML, diffs, scores, and model outputs. Only write REVIVAL.md
into the target at the final accepted state.

My strongest recommendation: make “minimal diff” a policy constraint, but make “progress” entirely evidence-based. The model can be brilliant at proposing a
modernization; it should never be able to redefine what counts as passing.

─ Worked for 2m 56s ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

---

## Session 2 — Cross-questioning the design

Human challenges raised:

1. Collection-time failures (envoy & vincent fail before any test runs)
2. Cost of fresh venv per iteration vs the 20-minute budget
3. Whether the set-inclusion rule could block a legitimate bug fix
4. What to keep vs defer for a 5-day, two-developer build

• You’re right to challenge this: collection failure should be a first-class state, not an awkward version of “zero tests passed.” I’m checking the exact artifact
behavior so the controller can be designed around the cases Envoy and Vincent actually hit.

• Searching the web

• Searched the web for site:docs.pytest.org junitxml collection errors pytest --junitxml ...

────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

• Yes—collection failure needs its own controller phase. For Envoy and Vincent, “0 passed” is not a score; it means pytest never reached the test runner.

## Collection failures and JUnit

If pytest starts normally and reaches session shutdown, --junitxml=... generally does produce XML for collection failures; pytest’s JUnit reporter records collection
failures as error entries. But it is not guaranteed when pytest itself cannot import, crashes, is killed on timeout, or fails before its reporting plugins initialize.
pytest JUnit implementation (https://docs.pytest.org/en/7.1.x/_modules/_pytest/junitxml.html), pytest exit codes
(https://docs.pytest.org/en/stable/reference/exit-codes.html)

I’d revise the design slightly:

- Use a tiny Necromancer-owned pytest capture plugin as the decision artifact.
- It records pytest_collectreport, collected node IDs, runtime reports, and session exit status into result.json.
- Also request JUnit XML for a portable, human-readable backup.
- Always capture stdout/stderr and have the parent process write process.json, even if pytest never launches.

That avoids parsing terminal prose for control decisions. Regex is then only for creating useful Coroner/Surgeon excerpts.

The candidate flow becomes:

install → pytest --collect-only with capture plugin
→ collection failed: score collection frontier only
→ collection succeeded: freeze node-id universe U
→ pytest full run with capture plugin + JUnit
→ score test progress

Do not use --continue-on-collection-errors for the main loop. Partial test results are noisier than useful while your actual blocker is an import error.

### Collection-frontier progress rule

A simple “collection errors must decrease” rule can deadlock too:

ImportError: requests.packages.urllib3
→ fix it
ImportError: collections.Mapping

The count remains one, but the first genuine blocker is gone.

Before collection succeeds, use:

bootstrap_score = (
collection_complete, # 0 or 1
collected_item_count,
-collection_error_count
)

Additionally allow a same-score frontier advance only if:

- at least one normalized active collection-error fingerprint disappeared;
- collection-error count did not increase; and
- every newly exposed fingerprint is novel in this run
  (not one already seen earlier in this run).

A fingerprint should be stable-ish: (exception type, importing module/test path, normalized first message line), not the entire traceback. This permits a dependency
chain to be uncovered while preventing A → B → A oscillation.

Once collection succeeds, freeze (U) and switch to the protected-pass score from the earlier design.

## Safe caching without reusing a dirty environment

A fresh venv per candidate does not have to mean 45 seconds of dependency work every time.

Use three layers:

harness venv fixed: Necromancer capture plugin + pytest tooling
dependency cache wheels, keyed by Python/platform/dependency fingerprint
candidate venv new and disposable for every patch

For a normal source-only patch:

1. Create a fresh candidate venv.
2. Install dependencies from the local wheel cache using pinned baseline constraints.
3. Install the current candidate project with --no-deps.
4. Run collection and pytest.
5. Delete the candidate venv.

The cache key should include:

Python major.minor + OS/architecture

- hash(requirements/pyproject/setup configuration)
- resolved dependency constraints

Do not reuse an installed candidate venv across test attempts. Tests and package installation can mutate it; that breaks isolation and makes failures irreproducible.

When a patch changes requirements\*.txt, pyproject.toml, setup.cfg, or setup.py dependency metadata:

- Treat it as a dependency-refresh attempt.
- Re-resolve once into a new wheel-cache key.
- Create a new pinned constraints snapshot.
- Run subsequent source-only candidates against that snapshot.

This is both faster and more reproducible than hitting PyPI on every iteration. It also makes the 20-minute budget plausible:

- First dependency resolution may cost 45–90 seconds.
- Later fresh-venv setup should usually be seconds, not a network resolution.
- The hard 20-minute deadline remains authoritative: “15 iterations” means at most 15 candidate evaluations, including repair attempts, not a promise to reach 15.

If test runs regularly approach their timeout, you will not get 15 meaningful attempts. For the demo, I’d cap at 8–10 evaluations and stop after three closed plan
items without accepted progress.

## When protected passes block a real fix

Yes, the set-inclusion rule can correctly block a real bug fix when the existing tests encode buggy behavior.

Concrete example:

# Existing, passing characterization test

def test_default_timeout_is_unbounded():
assert Client().timeout is None

The abandoned library has a genuine reliability/security bug: requests can hang forever. The correct revival changes the default timeout to 30 seconds:

assert Client().timeout == 30

That makes the formerly passing test fail. The set-inclusion rule rejects the code change even though the code is better.

This is not a flaw in the rule; it is a scope decision. The existing suite is simultaneously your oracle and a potentially stale specification.

For the hackathon, I recommend:

- Default: preserve every existing passing test. No test edits.
- Exception: Archaeologist may mark a plan item contract_change: true, list affected passing node IDs, and explain why.
- Controller does not auto-accept it. It produces needs_human_review.
- Optional later flag: --allow-contract-change <plan-id> permits that explicitly approved set to leave the protected-pass set and labels the PR/report as risky.

That protects your demo from an agent “fixing” behavior by redefining tests, while preserving a credible path for real semantic migrations.

## Five-day MVP ranking

Rank Build it If skipped
━━━━━━ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1 Fresh candidate snapshots, deterministic patch apply, discard-on-failure Failed attempts contaminate later attempts; rollback claims are unreliable.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
2 Collection-first state machine and capture plugin Envoy/Vincent have no usable feedback loop; the demo fails on your common case.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
3 Full pytest verification with structured results, plus logs The model effectively grades itself; you cannot prove a repo improved.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
4 Protected-pass progress guard and test/config immutability The agent can “win” by hiding tests, changing discovery, or regressing a
formerly passing test.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
5 Global deadline, subprocess process-group kill, command timeouts A hanging install/test can consume the entire repository budget.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
6 Wheel cache + fresh candidate venvs Fresh environments become too slow; reused environments make results
untrustworthy.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
7 Strict schemas for Coroner, plan, and patch proposal Malformed model output and ambiguous patches consume most of the engineering
time.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
8 One-repair-attempt state machine and duplicate-diff detection The loop burns calls repeating the same failed idea.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
9 Archaeologist plan with ordered fix IDs The agent makes broader, less explainable changes; still demoable, but weaker.
────── ────────────────────────────────────────────────────────────────────────── ──────────────────────────────────────────────────────────────────────────────────
10 Historian report and PR description The tool can revive code, but has a poor handoff story. Build this last.

Defer these unless time remains:

- Docker/Podman-grade sandboxing. Use clean temp directories, sanitized environment, and process limits for the benchmark.
- Flake detection via repeated baselines and targeted reruns.
- Sophisticated risk scoring, semantic diff analysis, multi-platform support.
- Automated contract-change approvals.
- General support for test migrations, package managers, and non-pytest runners.

With two developers, I’d split ownership this way:

- Developer 1: snapshots, venv/cache lifecycle, subprocess runner, capture plugin, scoring/controller.
- Developer 2: GPT schemas/prompts, Coroner/Archaeologist/Surgeon adapters, report generation, CLI/demo fixtures.

That division puts the hard truth-maintenance machinery ahead of the agent persona—which is exactly what Envoy and Vincent will force you to do.

─ Worked for 2m 08s ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
