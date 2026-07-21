# Necromancer

**A deterministic AI agent for reviving abandoned Python repositories.**

**Headline result:** GPT-5.6 revived [algorithms](docs/BENCHMARK.md) from a collection-blocked state to **61/61 passing tests** with one verified one-line patch.

[Repository](https://github.com/devaarav-iitd/Necromancer) · Demo video: (https://youtu.be/CLc-UfjBdpU)

 **OpenAI Build Week submission** · Track: Developer Tools
 Codex `/feedback` session ID: `019f715e-5bf2-7f91-a2db-a246236fea09`

## The problem

Many Python repositories still have users but no active maintainer. As Python, packaging tools, and dependencies move on, a once-useful project can rot: it no longer installs, test collection crashes, or a small compatibility break turns the suite red.

Necromancer is a deliberately narrow repair agent for that situation. It works on small Python repositories with pytest suites and treats the tests as the specification—not something the agent may rewrite to claim a win.

## How it works

The architecture has four GPT-5.6 roles around a deterministic Director:

1. **Coroner** diagnoses install, collection, and test evidence into a structured death certificate.
2. **Archaeologist** plans ordered migrations before code changes.
3. **Surgeon** proposes one minimal unified diff at a time.
4. **Historian** writes the final revival report and human-review notes.

The core rule is: **GPT proposes, deterministic code decides.** The Director snapshots the repository, applies an allowed patch only to a disposable candidate, reinstalls it, runs collection before the full suite, and promotes the candidate only when captured pytest evidence proves strict progress.

The score comes only from Necromancer's `result.json` test evidence: successful collection, protected passing node IDs, and debt for failures, errors, newly skipped tests, missing reports, or invalid/timeout artifacts. A candidate cannot trade a formerly passing test for a different passing test.

Before application, the anti-cheat policy rejects test files (including root-level `test_*.py`), `conftest.py`, pytest configuration, skips/xfails, test-selection flags, binary/mode/rename/delete changes, unsafe paths, oversized diffs, and mismatched preimage hashes. `git apply --check` then verifies that an allowed diff applies exactly—never fuzzily.

### Implementation status

The real GPT-5.6 Surgeon and deterministic Director are implemented and produced the benchmark below through the OpenAI Responses API with strict structured outputs. Coroner currently has a prompt and validated `DeathCertificate` schema, but is not wired into the CLI; Archaeologist and Historian remain architecture-stage placeholders. The four-role pipeline is the design target, not a claim that every stage ships today.

## The Graveyard Benchmark

Real GPT-5.6 Surgeon runs against four abandoned repositories. Every reported result is based on real pytest evidence.

| Repo | Cause of death | Outcome | Score (before → after) | Notes |
|---|---|---|---|---|
| algorithms | `fractions.gcd` removed in Python 3.9+ | ✅ full revival | (0,57,-1,0) → (1,61,0) | One AI-generated diff; 61 tests pass |
| envoy | Python-2 cascade (unicode, shlex, stdin) | 🟡 partial (6/9) | (1,0,-18) → (1,6,-6) | AI staged 3 fixes; at eval 3 it produced a malformed diff, and the apply-retry fed the git error back so GPT-5.6 corrected it at eval 4. Remaining 3: one is Python-2 syntax in a protected test file (flagged for human review); two surface only in test assertions with no in-repo source frame to target |
| vincent | dead `nose` import + removed `pkg_resources` | 🟡 partial | (0,0,-3,0) → (0,1,-2,0) | AI wrote a modern `importlib.resources` fix; remaining blocker needs a test-file edit, which the anti-cheat policy forbids |
| django-rest-swagger | dependency needs `pkg_resources`; Django settings unconfigured | ⚪ out of scope | (0,0,-4,0) → unchanged | Failure is in a dependency + test config, not the repo's own source; the agent correctly makes no change |

See [the full benchmark notes](docs/BENCHMARK.md).

## Why GPT-5.6 matters

GPT-5.6 supplies the autonomous reasoning that the deterministic engine intentionally does not: it reads captured traceback evidence, identifies a concrete source-side mechanism, and emits a minimal diff plus a SHA-256 preimage through the Responses API's strict structured-output boundary.

Envoy shows the value of the feedback loop. GPT-5.6 first emitted a malformed diff. The engine rejected it without applying it, returned the exact `git apply --check` feedback, and gave the same plan item a bounded retry. GPT-5.6 corrected the patch; only then did the engine run and score it. More attempts never relax the policy, preimage validation, fresh snapshots, or evidence-based acceptance.

## How Codex built it

Necromancer was designed and built with Codex as an engineering collaborator. The working record is in [CODEX_LOG.md](CODEX_LOG.md); the architecture decisions and their rationale are in [docs/architecture.md](docs/architecture.md).

That collaboration was useful precisely because it challenged our claims. Codex caught that the original anti-cheat rule protected `tests/` and `test/` directories but left a root-level `test_envoy.py` editable. We fixed the policy to protect `test_*.py`, `*_test.py`, and `conftest.py` at any depth before describing Envoy as honestly blocked by a protected test file.

## Setup in 2 minutes

Requires Python 3.11+.

```bash
git clone https://github.com/devaarav-iitd/Necromancer.git
cd Necromancer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Create `.env` in the repository root:

```dotenv
OPENAI_API_KEY=your_api_key_here
```

Run a real Surgeon against a local repository:

```bash
PYTHONPATH=src python -m necromancer revive /path/to/abandoned-repo \
  --surgeon real \
  --max-evaluations 8
```

For deterministic execution only (no model call):

```bash
PYTHONPATH=src python -m necromancer run /path/to/abandoned-repo
```

`--surgeon real` uses the OpenAI API, which is billed separately from ChatGPT/Codex product usage. Keep a small API balance available; Build Week Codex credits do not cover this project's API requests. [OpenAI billing guidance](https://help.openai.com/en/articles/8156019-i-want-to-move-my-chatgpt-subscription-to-the-api)

## Limitations

- Python only; the target must have a pytest suite.
- The isolated venv, subprocess timeouts, and fresh snapshots are appropriate for the trusted benchmark—not a Docker-grade security sandbox. Installing a repository and running its tests can execute arbitrary code.
- The shipped Surgeon is constrained to a supplied in-repo source file and is instructed not to edit dependencies or packaging configuration. The hard policy guarantee is narrower: it blocks tests and pytest infrastructure. Necromancer is not a general dependency or test-configuration migration tool.
- The anti-cheat policy is text-based. It is backed by evidence scoring and protected-pass set inclusion, but it is not a formal proof that a source patch cannot game a test.
- Collection-time dependency and configuration failures outside the repository's source are intentionally reported as partial or out of scope rather than patched around.

## License

MIT — see [LICENSE](LICENSE).
