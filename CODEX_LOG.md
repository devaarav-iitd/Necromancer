# Codex Usage Log

| # | Date | Session focus | Key decision / outcome | Time saved | Commits |
| 1 | 2026-07-17 | Architecture stress-test | Codex hardened the design: deterministic Director (GPT proposes, code decides), evidence-based progress via JUnit XML + set-inclusion rule, anti-cheat patch policy (no editing tests/), fresh-copy-per-candidate. Full transcript in docs/architecture.md | ~3h | (fill after commit) |


| 2 | 2026-07-17 | Architecture pushback | Challenged 4 decisions against our specific repos: forced collection-failure into a first-class state (this saves envoy & vincent, which fail before tests run), got a safe wheel-cache design to fit the 20-min budget, exposed the set-inclusion rule's stale-spec failure mode (timeout-default example), and got a ranked 5-day MVP build plan | ~2h | (fill after commit) |


| 3 | 2026-07-18 | Workspace + collection-first runner | Built immutable snapshots, pytest capture plugin, install/collect/run harness. Verified on envoy: runner correctly captures 9 collected tests all failing at CALL time with the unicode NameError (not a false "0 passed"). Baseline established for scoring. | ~3h | (hash) |


| 4 | 2026-07-17 | Verified runner on collection-time failure | Ran vincent through the runner: result.json correctly shows collection_complete=false, tests_collected=0, exit=INTERRUPTED, test phase not_started. Captured both root causes (dead nose import + missing pkg_resources) with full tracebacks. Runner now proven on both test-run (envoy) and collection-time (vincent) failure modes | ~1h | (hash) |


| 5 | 2026-07-18 | Built Coroner schema + prompt | Designed death-certificate Pydantic schema and Coroner prompt; validated against real envoy (py2_syntax, test_run, easy) and vincent (two causes, collection-time, moderate). Codex flagged that "nose" had no precise enum bucket; added missing_test_dependency cause_type and re-generated vincent's certificate to confirm | ~2h | (hash) |


| 6 | 2026-07-18 | Built + verified scoring layer | Implemented scoring.py (test score, bootstrap score, collection-frontier rule, protected-pass acceptance, error fingerprints). Unit tests pass against real artifacts: envoy baseline (1,0,-18), vincent bootstrap (0,0,-3,0). Added module docstring documenting the evidence-based principle | ~3h | (hash) |


| 7 | 2026-07-18 | Scoring edge-case characterization | Added tests for "collection succeeded but nothing ran" (xfail/dep-skip/empty). Found all three collapse to (1,0,0); determined this is correct neutral-state behavior, not a bug. Documented in architecture.md Session 3 | ~1h | (hash) |


| 8 | 2026-07-18 | Built + verified patch-apply and anti-cheat policy | Implemented patch_policy.py and patch_apply.py. Verified: real envoy unicode fix applied; edits to tests/, pytest.skip injection, and preimage-hash mismatch all rejected with correct reasons. Documented enforcement boundaries (text policy + protected-pass scoring as defense-in-depth) | ~2h | (hash) |


| 9 | 2026-07-19 | First full resurrection (algorithms) via Director | Ran the deterministic Director loop end-to-end on algorithms: baseline bootstrap (0,57,-1,0) with a fractions.gcd collection error → one-line patch (fractions.gcd → math.gcd) → final (1,61,0), 61 tests passing, promoted. Full deterministic pipeline (runner → policy → apply → score → promote) proven on a real repo | ~1h | (hash) |