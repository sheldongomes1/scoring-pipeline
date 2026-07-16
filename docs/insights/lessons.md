# Lessons Log

Canonical, dated lessons log. See `README.md` for the entry format an external
parser expects. Each entry begins `## YYYY-MM-DD — <one-line hook>` followed by
the lesson body. Chronological order.

---

## 2026-06-24 — The "go look over there" tell that a feature should be an agent

- Situation: Reviewing the `analyst_actions` layer, which tells a human analyst
  what to investigate after an anomaly flag (key_question, persistence_test,
  priority_section).
- What we assumed: that surfacing the right *instruction* to the user was the
  product. The four output fields are all errands handed to a human.
- Lesson: when your product's output is a list of "go check X over there"
  instructions, and the system already has a path to reach X (BigQuery, GCS
  narrative, an API), that errand is a candidate for an agent. The instruction is
  a smell that you stopped one harness short.
- Fix / rework: reframe the escalation section as an agentic investigation that
  reaches the data itself, with a disambiguation graph letting the user steer.
- Post angle: "If your AI feature ends every answer with 'now go check X
  yourself,' you probably stopped one tool call short of an agent."

## 2026-06-30 — Grounding is a precondition, not a co-equal axis

- Situation: Designing the judge that decides when an agentic investigation stops.
  The judge grades each output on three axes — Confirm/refute (C), Grounded (G),
  Open-questions (O) — giving an 8-state truth table to route on (loop / rerun /
  stop).
- What we assumed: that C, G, and O are three independent, co-equal signals you
  read off and combine. We filled the truth table treating an ungrounded output's
  "confirm/refute" verdict and "open questions" list as usable signal.
- Lesson: grounding is a **gate**, not a peer. If the output isn't traceable to
  source data (G=No), then C and O were produced by the *same* untrustworthy
  reasoning that failed the grounding check — so routing on them means routing on
  signals you just declared unreliable. Worse, "loop to gather more" on an
  ungrounded output burns the entire budget stacking garbage on garbage. The fix
  collapses 8 rows to one rule: if not grounded → repair (own small cap) then
  abandon; only *after* grounding passes do C and O decide loop-vs-stop.
- Fix / rework: reordered the policy so grounding short-circuits first. Surfaced a
  bonus third terminal state in the process — "inconclusive" (clean evidence,
  genuinely ambiguous) is distinct from "failed" and is a *useful* answer.
- Post angle: "Your LLM judge's 'is this correct?' score is worthless if the
  'is this grounded?' score failed — check provenance first, or you're scoring
  hallucinations."

## 2026-07-05 — The loop was fine; my test was lying to me (Python aliasing)

- Situation: Building the Phase-2 agent loop. To test it without a live model I
  wrote a scripted fake Anthropic client that records the args of every
  `messages.create()` call, so I could assert "on the 2nd call, the model was
  handed a tool_result that did NOT contain the provenance receipts."
- What broke / what we assumed: the assertion blew up with `'_TextBlock' object
  is not subscriptable`. I assumed the loop had mangled the message it sent. It
  hadn't. The agent loop reuses ONE `messages` list and mutates it in place
  (append assistant turn, append tool_result, loop). My fake stored
  `kwargs["messages"]` by reference — so after the run, every recorded call
  pointed at the *same* final list. Call #1's "snapshot" was actually showing
  call #4's state. The loop was correct; the observer was aliased to the thing it
  was observing.
- Lesson: a list in Python is a handle, not a value. Anywhere you log mutable
  state for *later* inspection — audit logs, event sourcing, test spies, undo
  stacks — you must snapshot at capture time (`list(x)` / `copy.deepcopy`), or the
  log silently reports the present as if it were the past. The bug hid as a loop
  bug for a minute because the symptom surfaced downstream of the real cause.
- Fix / rework: snapshot the messages list in the fake client's `_next`
  (`{**kwargs, "messages": list(kwargs["messages"])}`). 7/7 tests green.
- Post angle: "My agent-loop test failed and I spent a minute blaming the loop.
  The loop was perfect. My test spy was aliased to the mutable state it was spying
  on — it kept overwriting its own evidence. A list is a handle, not a value;
  snapshot before you log."

## 2026-07-05 — Proving 'agentic' with a diff, not a demo

- Situation: The loop closes and the model calls tools — but "it called a tool"
  doesn't prove *agentic*. A workflow calls tools too; the engineer just chose the
  calls at author-time. I needed the rigorous version of ADR-1's claim
  (path-variance conditioned on observations), not a feel-good single run.
- What we assumed / the trap: it's tempting to declare victory at the first live
  tool call. That's the workflow-in-disguise trap — a fixed DAG also emits tool
  calls; the data differs but the *step sequence* is identical run to run.
- Lesson: you prove an agent with a DIFF, not a demo. Run the SAME code on two
  inputs engineered to warrant different investigations, and show the tool-call
  *sequence itself* diverges as a function of what the model observed. If the two
  traces are identical, you built a workflow no matter how fancy the wrapper.
  Ours diverged sharply: on the recovered ticker (AAPL) the model ran a
  single-feature time-series sweep (offsets 0,−1,+1,+2,+3); on the still-broken
  ticker (WBD) it batched *multiple corroborating features* per call to explain
  *why* — different features, offsets, and batching, from one prompt.
- Fix / rework: `scripts/investigate_variance.py` runs both cases, reconstructs
  each tool-call signature from the transcript, and diffs them. The assertion is
  on the *paths*, not the answers.
- Post angle: "How do you prove your AI is an 'agent' and not a workflow with
  extra steps? Run the same code on two different inputs. If the tool-call
  sequence doesn't change, you built a workflow. Prove it with a diff, not a demo."

## 2026-07-05 — 'Two independent caps' means independent, not two numbers you add

- Situation: Building the ADR-1 judge, which owns loop termination. ADR-1 specified
  TWO budget caps — investigation_cap (bounds evidence-gathering) and repair_cap
  (bounds grounding reruns) — and stressed they must be *independent* so a repair
  explosion can't hide behind the investigation budget.
- What broke / what we assumed: I implemented "independent" as
  `ceiling = investigation_cap + repair_cap` — one summed turn limit. A unit test
  caught it: a no-judge run with investigation_cap=3 ran 5 turns. The bug: a run
  that can *never repair* (no judge attached) was still handed the repair headroom.
  Summing two budgets doesn't make them independent — it makes one bigger budget.
- Lesson: independence between two limits is a routing property, not an arithmetic
  one. investigation_cap must bound TOTAL turns (the real backstop); repair_cap is
  a SEPARATE sub-ceiling that trips its own terminal state (ABANDONED) on its own
  counter, regardless of remaining total budget. That's what stops a repair loop
  hiding behind the investigation budget — the thing ADR-1 actually asked for. If
  you add two caps together, neither is independent; you just renamed their sum.
- Fix / rework: `ceiling = investigation_cap`; `repairs` counted separately and
  compared to `repair_cap` for the ABANDONED route. 24/24 tests green.
- Post angle: "Spec said 'two independent budget caps.' I added them together.
  Independence between limits is about *which one trips and what happens* — not
  arithmetic. Add two caps and you don't have two caps, you have one bigger one."

## 2026-07-05 — The judge that owns 'stop' catches a distinction a flag can't

- Situation: First live run of the two-model loop — Opus generator proposes, Sonnet
  judge owns the stop decision (grounds each figure by re-fetch, then grades). Ran
  it on WBD, whose cash conversion stayed broken and whose ocf_to_assets was
  feature_missing for every quarter.
- What we assumed / the risk: a judge deciding "are there open questions?" would
  see missing data and demand it — looping forever on data that doesn't exist.
- Lesson: giving the judge the 3-state enum (found / feature_missing /
  period_not_filed) rather than a null lets it reason about WHY something is absent.
  Sonnet correctly ruled the missing ocf_to_assets a "noted limitation, not an
  open answerable question central to the predicate" and returned RESOLVED. A
  bare-null result would have looked identical to a fetchable-but-missing value,
  and the judge would have looped. The enum you designed three ADRs ago is what
  makes the termination logic correct under a second model.
- Fix / rework: none — it worked first try because the contract carried the
  distinction. This is the ADR-2 status-enum decision cashing out downstream.
- Post angle: "My AI investigator's judge saw missing data and had to decide: is
  this a dead end or a to-do? It got it right — because three design decisions ago
  I refused to represent 'missing' as null. Contracts you set early decide whether
  your agent loops forever."

## 2026-07-11 — The 'generic' component that was secretly fitted to one case

- Situation: Adding the second tool (narrative sections) to an agent that already
  had one (numeric feature history). The tool registry looked fully generic —
  `ToolRegistry(bindings, feature_keys)` routed any tool by name.
- What broke / what we assumed: it passed ONE `feature_keys` list to every tool's
  schema builder. Fine with one tool. The second tool's vocabulary is *section
  names*, not feature names — so the "generic" registry was silently assuming all
  tools share one enum. It only looked generic because there had only ever been one
  tool to fit.
- Lesson: an abstraction validated against a single case isn't proven generic —
  it's proven to fit that case. The second instance is the real test, and it
  routinely exposes a shared assumption the first one hid. Fix: make each binding
  self-contained (it carries its own already-built schema, enum baked in by whoever
  knows that tool's vocabulary); the registry then holds no vocabulary at all.
- Post angle: "My tool registry looked perfectly generic — until the second tool.
  One shared config field was quietly assuming every tool spoke the same
  vocabulary. An abstraction tested on one case isn't generic; it just fits. The
  second case is the test."

## 2026-07-11 — I made grounding polymorphic and forgot its twin

- Situation: The narrative tool returns prose, so the judge's grounding gained a
  second 'head' — deterministic `==` for numbers, a model semantic-check for
  passages — dispatched by a `grounding_mode` tag on each evidence item.
- What broke / what we assumed: I upgraded the grounding path to handle both
  evidence types, felt done, and started the live demo. But the judge's *other*
  step — the confirm/open grader — still built its evidence view assuming every
  item was a numeric result (`e.feature`, `e.value`, `e.resolved_report_date`). The
  moment a real investigation gathered a number AND a passage, that grader would
  `AttributeError`. My unit tests missed it because they called the grounding
  method directly, never the full grade path with mixed evidence.
- Lesson: when you make one code path polymorphic over a new type, grep for EVERY
  path that touches that type — the loud one you're editing and the quiet siblings
  that still assume the old shape. And write the test at the level that actually
  exercises the integration (the full `evaluate()`, not just the sub-method), or
  the gap hides. Caught it building the demo; added the mixed-evidence regression.
- Post angle: "I taught my judge to verify two kinds of evidence and shipped it.
  It would've crashed on the first real run — I upgraded the grounding step and
  forgot its twin, the grader, still assumed one type. Make one path polymorphic,
  audit all of them. Test at the integration level or the gap hides."

## 2026-07-11 — The grounding gate made the agent retract a claim, live

- Situation: First live run of the full disambiguation graph — a WBD flag fanned
  out into four competing hypotheses; we steered into the working-capital one and
  ran the focused deep-dive (both tools + grounding judge).
- What happened: the investigator initially inferred that the ABSENT MD&A sections
  "would have contained" working-capital detail — an unsupported claim about
  evidence it never had. The judge's semantic grounding head pushed back. The agent
  re-examined, wrote "I should not have framed the absent sections as places that
  'would have contained' detail — that was an unsupported inference. I retract it,"
  and downgraded its verdict to "cannot confirm with available tools" rather than
  invent receivables data it never fetched.
- Lesson: a grounding gate isn't just a pass/fail stamp on a finished answer — when
  it rejects, it forces the generator to re-ground and walk back overreach. The
  visible payoff of provenance-first design (ADR-2/ADR-5/ADR-6/ADR-7) is an agent
  that says "I can't prove this with the tools I have" instead of hallucinating the
  gap. The honest null is the feature, and it only exists because grounding is a
  precondition the model can't talk its way past.
- Bonus product signal: the agent couldn't fully test the working-capital or
  revenue-recognition hypotheses because the toolset exposes no balance-sheet
  line items (receivables/payables). The investigation surfaced its own next tool.
- Post angle: "I watched my AI investigator try to confirm a hypothesis, get called
  out by its own grounding judge, and retract the claim — then say 'I can't prove
  this with the tools I have' instead of inventing the data. The honest 'I don't
  know' is the product, not a failure of it."

## 2026-07-11 — A flag isn't a task, it's a fork you should let the user see

- Situation: Building the disambiguation graph (Phase 3). The instinct was to make
  the agent investigate a flag. The mission reframed it: a flag is AMBIGUOUS — a
  cash-conversion collapse has four plausible causes — and the single-branch loop
  silently commits to one, burying the choice in its first tool call.
- Lesson: the interface win isn't running the investigation faster — it's exposing
  the hidden assumption (which explanation the agent bet on) as a visible, steerable
  branch BEFORE spending budget. So the fan-out is a steering surface, not a
  parallelism optimization. "Investigate all N and show results" is the tempting
  engineer answer and it's wrong: four finished reports is a fait accompli, not a
  choice — it spends budget to REMOVE the human's chance to correct the assumption.
  Propose cheap (name the hypotheses), let the human steer, then go deep on one.
- Post angle: "The obvious way to build an AI that handles an ambiguous request is
  to make it pick an interpretation and run. The better way is to make it show you
  the interpretations and let you pick — before it spends a dollar. Fan-out isn't
  parallelism; it's a steering wheel."

## 2026-07-11 — The agent wrote its own feature request, and it resolved on the retry

- Situation: A live disambiguation run stalled — the working-capital branch came
  back "cannot confirm with available tools; the toolset has no receivables/payables
  data." Rather than treat that as a failure, we read it as a spec: the agent had
  named the exact tool it lacked.
- What we did: built tool #3 (balance-sheet line items), wired it in, and re-ran the
  IDENTICAL branch. It resolved cleanly — the agent fetched receivables/payables,
  found them moving as a small cash source (not a drain), REJECTED the working-capital
  hypothesis on real numbers, and pointed to content amortization instead.
- Lesson: a grounded agent's "I can't answer this with what I have" is not noise —
  it's a precise capability request, emitted by the system's own reasoning. If your
  agent is honest about its limits (grounding-first design), its dead-ends become
  your roadmap. The tightest feedback loop in the build was: agent hits wall → wall
  names the missing tool → build it → same query resolves. That loop only exists
  because the agent refuses to hallucinate past the gap.
- Post angle: "My AI investigator hit a wall and told me exactly which tool it was
  missing. I built that one tool, re-ran the same question, and it resolved. An
  honest agent's 'I can't' is a feature request in disguise — but only if it won't
  fake the answer instead."

## 2026-07-11 — 'The one X' is a bet that there'll only ever be one X

- Situation: Adding tool #3 (a second STRUCTURED tool). Its results reuse
  FeatureResult and are grounded deterministically, so "add a tool, zero judge
  change" (ADR-7) should have held. It didn't, quite.
- What broke: the judge's grounding re-fetched through a SINGLE `reverify` callable
  — which was the first tool's backend. A balance-sheet value re-verified against
  the feature backend finds nothing. The lone `reverify` was a hidden bet that there
  would only ever be one structured backend.
- Lesson: this is the SECOND time this exact shape bit in one project — the registry
  first assumed one `feature_keys` vocabulary, now the judge assumed one `reverify`
  backend. The pattern: any singleton named "the X" (the config, the backend, the
  client, the vocabulary) is an unstated assumption that X is unique, and the moment
  a second X appears it breaks. Fix both times was the same move: route by an
  identity the data already carries (tool name; provenance.source) instead of
  hard-wiring the one. When you write "the reverify" / "the source," ask: what
  happens on the second one?
- Post angle: "Twice in one project the same bug shape bit me: 'the config,' then
  'the backend.' Any singleton called 'the X' is a silent bet there'll only ever be
  one X. The fix both times: route by an id the data already carries. When you name
  something 'the,' ask what the second one does to you."

## 2026-07-12 — The recursion needed the same brake I'd already built for the loop

- Situation: Phase 4 — letting a resolved branch spawn deeper child branches, so the
  investigation graph grows into a tree. Asked "what stops the recursion?", the
  instinct was: it stops when no branch raises a new question.
- What that misses: that's a purely SEMANTIC stop — "trust the system to run out of
  questions" — which is the exact bet ADR-1 rejected for the single loop. A chain
  can plausibly emit "one more question" forever. And at the graph level it's WORSE:
  growth is exponential (breadth^depth), not linear, so an unbounded tree is a cost
  bomb, not a slow leak. I'd literally debugged the independent-caps version of this
  two phases earlier (don't SUM the two budgets) — and nearly reproduced the missing
  half one level up.
- Lesson: termination conditions don't automatically inherit when you add a level of
  recursion — you have to consciously transfer them. The loop's pattern (semantic
  stop + an INDEPENDENT hard cap that guarantees it ends at all) is exactly what the
  graph needs, just renamed: semantic = "no new questions proposed," hard =
  max_depth AND a global node budget, each its own ceiling. Same shape, one level up.
  And the grounding-as-precondition rule transfers too: only a RESOLVED branch spawns
  children — you don't build a deeper investigation on a foundation you couldn't
  ground (extending an ABANDONED branch is ADR-1's "loop on ungrounded output" again).
- Post angle: "I built a careful termination condition for my agent's loop — semantic
  stop plus a hard budget cap. Then I added recursion on top and almost forgot: the
  recursion needs its OWN brake. Termination doesn't inherit when you add a level.
  And a tree explodes exponentially, so the hard cap matters more, not less."

## 2026-07-12 — My fakes hid a SEV-1, and a different model found it

- Situation: Before running a 520-call batch, I had a second model (a different
  family) do an adversarial audit of the whole system — the same "a separate judge
  decorrelates blind spots" principle the product is built on, turned on my own work.
- What it found: three real defects in the grounding gate — the exact mechanism the
  product's "won't hallucinate" promise rests on. The worst: against REAL BigQuery,
  the most common analyst query ("+1 quarter — did it recover?") re-grounded
  authentic "not-filed" evidence as a wrong value and abandoned the investigation.
  My in-memory FAKE masked it, because the fake's calendar was tidy where real
  filings are not. A second: I'd been claiming "deterministic, un-gameable grounding"
  — but the code only checked the evidence was authentic, never that the numbers the
  model WROTE matched the evidence. For numbers-only investigations the
  anti-hallucination guarantee wasn't implemented; a demo happened to catch a
  narrative overreach and I generalized from luck.
- Lesson: (1) fakes prove a mechanism CLOSES; they do not prove it's CORRECT against
  real data — the fake's convenient regularities are exactly where the integration
  bug hides. Budget a real-data pass before trusting a faked mechanism. (2) Beware
  your own confident ADR prose: "un-gameable" was rhetoric that outran the code, and
  it took an outside model to separate the claim from the implementation. (3) An
  adversarial review by a different model family, run BEFORE the expensive operation,
  is cheap insurance that pays for itself — it is literally the design principle of
  the thing I was building, applied to the building of it.
- Fix / rework: ADR-13 — provenance replays the request; an always-on answer↔evidence
  head; integrity failures made unrepairable. Full findings + status:
  docs/insights/audit-2026-07-12-fable.md.
- Post angle: "I asked a different AI model to audit my AI system before a costly
  run. It found a critical bug my own test fakes had hidden — and caught me claiming
  'un-gameable grounding' the code didn't implement. Fakes prove your thing runs, not
  that it's right. Have a different mind check before you spend."

## 2026-07-14 — I bet on the wrong latency fix; the measurement overruled me
- Situation: the deep-dive investigation took ~150-260s and the user called it
  unacceptable. Asked which model to swap and whether to parallelize, I answered
  confidently: the bottleneck is the Opus generator turns, swap to Haiku, that's the
  big rock. I did not measure first.
- What broke / what I assumed: an instrumented run showed the real split — the JUDGE'S
  re-verification against BigQuery was 48% of wall-clock (124.9s), the generator turns
  only 31% (82s). My "big rock" was the second rock. The cause: the grounding gate
  re-fetched evidence one item at a time — 39 sequential BigQuery queries, a fresh
  client built (and thrown away) each time, re-run on the whole pile every repair
  cycle. And the sting: that 125s blew the 240s wall-clock guard, so the investigation
  terminated CAPPED / trusted=False. The latency bug was silently producing UNTRUSTED
  answers.
- Lesson: a plausible latency hypothesis from someone who knows the system is still a
  guess. "Measure before you optimize" isn't a platitude — I'd have swapped the model,
  shaved 60s off a 262s problem, and shipped something that still randomly failed to
  earn trust. And the deeper one: a latency bug and a correctness bug can be the SAME
  bug. When re-verification eats the time budget, the safety timeout converts slow runs
  into untrusted ones. Confirmed empirically post-fix: the exact branch that returned
  CAPPED/untrusted at 262s now returns resolved/trusted.
- Fix / rework: batch the reverify — group by (source, ticker), one whole-history query
  per source (the backend already pulls the full history), a session-level memo across
  repair cycles, shared BQ/GCS clients, thread-pool the rest. Byte-identical grounding
  semantics (the == comparison and the failed-list order are untouched; only the query
  count changes: 39 → 2). Measured 124.9s → 3.6s.
- Post angle: "I told the team which fix would speed up our AI agent. Then we measured —
  and I was wrong. The real bottleneck was 48% of the runtime hiding inside the safety
  check, and it was quietly making the system's own answers untrustworthy. A latency bug
  and a correctness bug were the same bug. Measure before you optimize, even when you're
  sure — especially when you're sure."

## 2026-07-14 — "required" in an LLM tool schema is a hint, not a runtime contract
- Situation: the live "Investigate" button 500'd intermittently in production. The
  engine threw `KeyError: 'unsupported_claims'` mid-investigation, ~90s in.
- What broke / what I assumed: the grounding judge's answer-support check forces the
  model to call a tool whose schema marks `unsupported_claims` as `required`, then read
  `payload["unsupported_claims"]`. When the judge concluded the answer WAS supported, it
  returned `{supported: true}` and simply omitted the empty array. The bare subscript
  threw; the service's blanket `except` turned it into a 500 that killed the whole ~150s
  run. I'd trusted the schema's `required` as a runtime guarantee.
- Lesson: `required` in a tool/function schema steers the model; it does not bind it.
  Forced tool-use still returns partial inputs — especially "optional-feeling" fields the
  model judges irrelevant to the current case. Parse model output defensively at the
  boundary: `.get()` with fail-closed defaults, never bare subscripts. The model IS
  untrusted input; validate it like any other.
- Fix / rework: `payload.get("supported", False)` (missing verdict fails closed),
  `payload.get("unsupported_claims") or []`. One line per field; crash gone.
- Post angle: "Our AI's 'required' output field went missing and crashed a two-minute job
  in production. LLM tool schemas are hints, not contracts — the model drops fields it
  thinks don't apply. Treat model output like any untrusted input: parse defensively,
  fail closed."

## 2026-07-14 — "It's not deployed anywhere" — I shipped to the wrong project
- Situation: shipping the new investigation UI to production. A prior scan had concluded
  redink-ui "wasn't deployed anywhere," so I stood up a fresh Cloud Run service and wired
  it all up. The user then said: "I can't log in — I usually use tryredink.dev."
- What broke / what I assumed: tryredink.dev had been live the whole time, served by a
  redink-ui Cloud Run service in a THIRD GCP project (qqq-anomaly-lab) behind a domain
  mapping I never checked. My "not deployed" conclusion looked at two projects and
  Firebase Hosting and stopped. My fresh service was an orphan — real prod, real Firebase
  auth (authorized domains), real users, all one indirection away from where I looked.
  The login failure was the tell: my service's URL wasn't an authorized Firebase domain;
  the real one was.
- Lesson: "where does this run in prod?" is answered by tracing the user's real entry
  point — the domain they type — not by enumerating the services you happen to know. A
  domain mapping, a load balancer, a different project: the deploy target hides one hop
  away from the code. Verify infra state against the live URL before you touch it. (Same
  day, two more of the genre: granted BigQuery `jobUser` on the service's own project when
  the client pinned a DIFFERENT billing project — 500 until fixed; and a masked deploy
  failure where `$?` read a trailing `grep` instead of the `gcloud` that an SSL blip had
  silently killed.)
- Fix / rework: deleted the orphan; deployed the feature into the real qqq-anomaly-lab
  service the domain maps to, preserving its SA/IAM/memory and adding only the new code +
  env vars.
- Post angle: "I convinced myself our app 'wasn't deployed anywhere' and spun up a new
  server. It had been live all along — one domain-mapping hop away, in a project I never
  checked. Find where prod actually runs by following the URL your users type, not the
  services you remember."

## 2026-07-14 — Fast backend isn't enough; make the wait legible
- Situation: after cutting the reverify path and reaching trusted results, the run still
  took ~200s of real LLM work. The user wanted it to feel acceptable.
- What broke / what I assumed: I first framed this as a pure latency problem. But an
  agentic loop with real model turns has a hard floor — five-to-seven sequential LLM turns
  cannot be 2 seconds, no matter the optimization. Chasing raw speed alone never reaches
  "acceptable," and worse, a fast run that ends untrusted is a downgrade.
- Lesson: "total latency" and "the experience of waiting" are different problems with
  different fixes, and the second often matters more. A 45s blank spinner is unacceptable;
  45s (or 200s) of watching the agent fetch filings, ground 29 facts, fail a check and
  repair, then resolve — that's the product telling its own story. Perceived latency is
  the higher-leverage fight once the backend is reasonable. Verified: SSE frames stream
  incrementally through Cloud Run (first frame 0.5s, 43 events), so the wait is now a
  live investigation, not a hang.
- Fix / rework: the loop emits turn/tool/grounding/verdict events; a new SSE endpoint
  runs the blocking loop in a worker thread and drains an event queue with heartbeats;
  the UI renders a live growing step feed and reuses the existing verdict rendering +
  trusted banner for the final frame. The decoupled generator/judge architecture is also
  what lets a cheap model run the hot path later without losing correctness — the judge
  enforces grounding regardless of who generated.
- Post angle: "We fixed our AI agent's biggest bottleneck and users still called it slow —
  because they were staring at a blank spinner. The fix wasn't more speed; it was streaming
  the agent's work so people watch it think. Fast AND legible beats fast alone."

## 2026-07-16 — The judge's own reasons showed it rejects analysis, not fabrication
- Situation: re-ran the 6-ticker eval on the post-fix stack (batched reverify, BQ balance
  sheet) after instrumenting the capture with `judge_reasoning`/`ungrounded_items`. The
  ADR-15 prediction held (PANW: capped/untrusted → resolved/trusted; VRSK: 633s → 214s),
  but the trusted-rate did NOT recover: still 2/6.
- What broke / what I assumed: I assumed grounding failures meant the generator was
  fabricating. Reading the judge's actual reasons, of ~11 flagged items across the 3
  ABANDONED runs only ~3 are genuine catches (e.g. VRSK: generator conflated a quarterly
  -1.06% metric with the filing's -50.7% 9-month decline). The rest: rejecting HEDGED
  framing (INSM was flagged for calling financings "consistent with, not confirmed" — the
  judge itself says they're "the correct chronological explanation"), demanding
  plausibility cross-checks of values the INTEGRITY head already re-fetched (VRSK), and —
  the systemic one — flagging the generator's truthful description of its own tool calls
  ("period_offset", "resolved_report_date") as "an invented schema not present in the
  evidence" (STX). ADR-5's visibility split CAUSES that last class: the judge sees only
  receipts, so the generator narrating its own process is unverifiable by design. Also:
  INSM flipped resolved/trusted → abandoned/untrusted on identical code+data — judge
  churn — so a trusted-rate gate at n=6 is statistically meaningless for the ADR-16 A/B.
- Lesson: instrument the VERDICT REASONS before tuning anything. "Trusted-rate is low"
  looked like a generator-quality problem and was actually a judge-calibration problem
  in three separable classes, each with a different owner: rubric wording ("flag any
  characterization the passages do not support" invites style rejection), architecture
  (ADR-18's structured output removes process narration from the judged surface), and
  statistics (verdict churn means gates need bigger n or repeated runs).
- Fix / rework: pending — rubric fix is a checkpoint decision. ADR-18 implementation
  removes the STX false-positive class structurally.
- Post angle: "Our AI safety check kept rejecting our AI's answers. We finally logged the
  judge's reasons: only 3 of 11 rejections were real. It was punishing careful hedging as
  if it were lying. If your evaluator's verdicts aren't themselves inspected, you're not
  measuring your agent — you're measuring your judge."

## 2026-07-16 — A wall-clock guard between turns can't stop a 21-hour turn
- Situation: investigation 6 of the eval run (FTNT) sat for 77,102 seconds — 21.4 hours —
  before terminating CAPPED. The `max_seconds=240` guard never fired.
- What broke / what I assumed: I assumed max_seconds bounded the run. It bounds the LOOP —
  checked between iterations — so a single blocking client call (Anthropic or
  BigQuery/GCS; the capture can't say which) is unbounded. One hung socket froze the
  whole eval overnight; the guard only woke up when the call finally returned.
- Lesson: a timeout that is checked cooperatively is a lower bound, not an upper bound.
  Every network client inside the loop needs its own hard timeout (SDK request timeout +
  bounded retries); the loop-level guard is then the aggregate ceiling, not the only
  defense. Corollary for evals: one unbounded call serializes the whole batch.
- Fix / rework: pending — add per-client timeouts (anthropic `timeout=`, BQ/GCS retry
  deadlines) and log which call was in flight when a turn exceeds budget.
- Post angle: "Our AI agent had a 4-minute timeout and still ran for 21 hours. The
  timeout was checked between steps — and one step never came back. Cooperative timeouts
  are suggestions; only socket-level deadlines are promises."

## 2026-07-16 — Structured outputs turned the token limit into a protocol hazard
- Situation: first live run of the ADR-18 loop (18-run eval). PANW passed 3/3 through
  the full new path; INSM r1 died with a 400 — `tool_use` ids without `tool_result`
  blocks — poisoning the transcript for every subsequent request in that run.
- What broke / what I assumed: `DEFAULT_MAX_TOKENS=2048` predates ADR-18, when the final
  answer was prose — truncated prose degrades gracefully. ADR-18 made the final turn a
  large JSON tool call (verdict + rationale + 3–6 citation objects + caveats), so the
  same limit now cuts the model off MID-tool_use: `stop_reason="max_tokens"` with a
  dangling tool_use block. The stall branch matched on `stop_reason != "tool_use"`,
  appended a bare text nudge, and left the block unanswered — an API-contract violation.
  PANW's terse answers fit in 2048; INSM's hedge-heavy long answer didn't, so the bug
  selected for exactly the borderline cases the eval was measuring.
- Lesson: when you move model output from prose to structure, truncation changes
  category — from a quality problem to a protocol violation. Any branch keyed on
  `stop_reason` must still honor invariants carried by the CONTENT (every tool_use id
  needs a tool_result), because stop_reason and content are not exclusive. Handle the
  general case (any non-tool_use stop carrying tool_use blocks), not just max_tokens.
- Fix / rework: dangling tool_use blocks now get an `is_error` tool_result telling the
  model it was cut off and to re-issue; DEFAULT_MAX_TOKENS 2048 → 4096. Regression test
  with a scripted truncated turn. Caught live within one eval run because the harness
  records per-run errors and PANW/INSM diverged immediately.
- Post angle: "We upgraded our AI agent from prose answers to structured JSON — and our
  token limit instantly became a protocol bug. A truncated sentence is a shrug; a
  truncated JSON tool call is a corrupted conversation. Structure changes what failure
  means."
