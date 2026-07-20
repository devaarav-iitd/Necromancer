# Necromancer — Graveyard Benchmark

Real GPT-5.6 Surgeon run against four abandoned Python repositories. The agent proposes fixes; a deterministic engine verifies every patch against real test evidence and can never be told success by the model.

| Repo | Cause of death | Outcome | Score (before → after) | Notes |
|---|---|---|---|---|
| algorithms | `fractions.gcd` removed in Python 3.9+ | ✅ full revival | (0,57,-1,0) → (1,61,0) | One AI-generated diff; 61 tests pass |
| envoy | Python-2 cascade (unicode, shlex, stdin) | 🟡 partial (5/9) | (1,0,-18) → (1,5,-8) | AI staged 2 accepted fixes; engine rejected a 3rd that didn't improve the score |
| vincent | dead `nose` import + removed `pkg_resources` | 🟡 partial | (0,0,-3,0) → (0,1,-2,0) | AI wrote a modern `importlib.resources` fix; remaining blocker needs a test-file edit, which the anti-cheat policy forbids |
| django-rest-swagger | dependency needs `pkg_resources`; Django settings unconfigured | ⚪ out of scope | (0,0,-4,0) → unchanged | Failure is in a dependency + test config, not the repo's own source; the agent correctly makes no change |

Every outcome is grounded in real pytest evidence. The mixed results are deliberate — they show the agent's **capability** (algorithms), its **multi-step reasoning** (envoy), its **integrity** (vincent — won't edit tests), and its **scope-awareness** (django-rest-swagger — won't hack dependencies).