# Necromancer — Graveyard Benchmark

Real GPT-5.6 Surgeon run against four abandoned Python repositories. The agent proposes fixes; a deterministic engine verifies every patch against real test evidence and can never be told success by the model.

| Repo | Cause of death | Outcome | Score (before → after) | Notes |
|---|---|---|---|---|
| algorithms | `fractions.gcd` removed in Python 3.9+ | ✅ full revival | (0,57,-1,0) → (1,61,0) | One AI-generated diff; 61 tests pass |
| envoy | Python-2 cascade (unicode, shlex, stdin) | 🟡 partial (6/9) | (1,0,-18) → (1,6,-6) | AI staged 3 fixes; at eval 3 it produced a malformed diff, and the apply-retry fed the git error back so GPT-5.6 corrected it at eval 4. Remaining 3: one is Python-2 syntax in a protected test file (flagged for human review); two surface only in test assertions with no in-repo source frame to target |
| vincent | dead `nose` import + removed `pkg_resources` | 🟡 partial | (0,0,-3,0) → (0,1,-2,0) | AI wrote a modern `importlib.resources` fix; remaining blocker needs a test-file edit, which the anti-cheat policy forbids |
| django-rest-swagger | dependency needs `pkg_resources`; Django settings unconfigured | ⚪ out of scope | (0,0,-4,0) → unchanged | Failure is in a dependency + test config, not the repo's own source; the agent correctly makes no change |

Every outcome is grounded in real pytest evidence. The mixed results are deliberate — they show the agent's **capability** (algorithms), its **multi-step reasoning** (envoy), its **integrity** (vincent — won't edit tests), and its **scope-awareness** (django-rest-swagger — won't hack dependencies).