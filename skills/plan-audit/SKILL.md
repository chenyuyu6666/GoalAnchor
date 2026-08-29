---
name: plan-audit
description: >-
  Gate a formed external plan, patch, or agent conclusion before Codex investigates
  or executes it. Also use when the user explicitly authorizes Autonomous Closure or
  end-to-end autonomous convergence. Do not use for ordinary tasks without either
  condition.
---

# Plan Audit

Use `PLAN -> VERIFY -> EXECUTE` when an external proposal may be adopted, the user
requests an audit, or the user explicitly enables Autonomous Closure. Ordinary
questions, explanations, exploration, code reading, and direct low-risk tasks without
one of these conditions do not trigger it.

## PLAN

Static only: use the task and supplied plan. In Autonomous Closure without a formed plan,
use only the task, user-accepted constraints, and facts already supplied by the user to
draft the first plan. Do not edit, run, build, test, deploy, browse for proof, or inspect
the repository.

Report each as `【是】` or `【否】` with a concrete reason:

- `Q1 Goal`: objective, constraints, blocker, and completion condition; no substitution
  or unsupported scope.
- `Q2 Logic`: complete causal problem -> action -> expected result.
- `Q3 Path`: assumptions rechecked; no stacking on an unconfirmed patch, workaround,
  failed plan, or prior agent inference.
- `Q4 Minimum`: every step necessary; no unrelated research, dependency, abstraction,
  or cleanup.
- `Q5 Structure`: no clear over-design, duplicate responsibility, implicit coupling,
  public-path hack, or needless regression surface.

Five `【是】`: `✅ PLAN AUDIT PASS`. Otherwise `❌ PLAN AUDIT FAIL`, name failures, give
the smallest repair, print `STOP`, and do not execute. In ordinary mode this stops the
current plan. In Autonomous Closure, it stops execution of the failed plan but permits
an autonomous minimal revision and a return here. PASS permits verification only; it is
not proof.

## VERIFY

Enter only after PLAN PASS. Investigate only uncertainty that can change implementation,
acceptance, or execution safety. Before each bounded investigation state:

1. What is verified.
2. Why it affects the decision.
3. One observable, finite `EXIT_CRITERION`.

Define the criterion before the first action. It is the only semantic stop condition;
do not broaden open sets such as "all relevant logs" or move the goalpost. If evidence
invalidates it or a core assumption, end the item and return to the VERIFY decision
point; do not rewrite it to continue.

An item cannot launch another item. A new local uncertainty may start a new bounded item
only after that return and only if problem, requirements, core assumptions, main strategy,
acceptance, and deliverable remain unchanged. Otherwise return to PLAN; the new item gets
its own three statements. Do not investigate for completeness.

Use a real delegated agent for narrow independent checks when available; never invent
delegation or treat its conclusion as proof. Inspect reproducible evidence and state the
boundary if delegation is unavailable.

Success: `✅ VALIDATION PASS`. Refutation: `❌ STAGE 2 VALIDATION FAIL`, never
`PLAN AUDIT FAIL`; PLAN already passed. A changed plan returns to PLAN.

## EXECUTE

Enter only after VALIDATION PASS. Run minimum relevant preflight, preserve recovery where
needed, make the smallest change, review the diff, and collect deliverable evidence. For
firmware/build artifacts use fresh build, package, and board/runtime validation when
applicable. Decide continue, revise, roll back, or stop.

`PLAN_CHANGED=YES` when objective, requirements, core assumptions, main strategy,
acceptance, evidence obligations, or deliverable scope changes; old PLAN PASS expires
and the new plan returns to PLAN. Pure spelling, formatting, and strictly equivalent
wording do not change it. Investigation returns to VERIFY and never recursively spawns or
silently changes plan meaning.

## AUTONOMOUS CLOSURE

Enable only when the user explicitly authorizes autonomous convergence, implementation,
or end-to-end closure. This transfers ownership of intermediate revisions; it does not
change any Gate, status, evidence, or investigation rule above.

- If no formed plan exists, create the smallest complete plan from the task, accepted
  constraints, and supplied facts. It receives no special trust and must pass all Q1-Q5
  before VERIFY.
- On PLAN failure, state failed Qs, create the smallest decision-relevant replacement,
  and rerun Q1-Q5. Never execute while it fails; stop and escalate when the task is
  ambiguous or revisions only reword the same plan or add complexity.
- When VERIFY, preflight, build, or runtime evidence exposes a technically decidable
  correction, diagnose it, make the smallest repair, and rerun the earliest affected
  stage or check. A
  material plan change returns to PLAN; never stack repairs on an unconfirmed assumption.
- Do not escalate an intermediate correction or a choice among valid options; choose the
  smallest compliant option. Escalate only genuine unresolved ambiguity or an external
  decision the evidence cannot settle. Treat user-declared frozen facts as baseline and
  report build/package/validation success only as the evidence it actually supports.
