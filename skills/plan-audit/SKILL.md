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

Keep the final `TASK_OBJECTIVE` separate from any `CURRENT_BLOCKING_UNCERTAINTY`.
A diagnostic or A/B step is a means to advance the task, not the task's completion
condition, unless the user explicitly scoped the task to diagnosis.

Before Q1-Q5, state exactly:

```text
TASK_OBJECTIVE: the final implementation, delivery, or acceptance outcome.
CURRENT_BLOCKING_UNCERTAINTY: the specific unknown blocking the next reliable step, or NONE.
PROPOSED_STEP_AND_DECISION_LINK: the next action and how each possible result changes implementation or acceptance.
```

Never use the uncertainty or the proposed step as the task objective.

Report each as `【是】` or `【否】` with a concrete reason:

Use the labels `Q1 Goal`, `Q2 Logic`, `Q3 Path`, `Q4 Minimum`, and `Q5 Structure`
exactly and in this order; do not replace them with synonyms.

- `Q1 Goal`: final objective, constraints, blocker, and completion condition; no
  substitution or unsupported scope. A bounded diagnostic first step is allowed, but
  it must not replace the objective or become the completion condition. If the task
  requires implementation or acceptance closure, the plan must state how each diagnostic
  outcome returns to an implementation or acceptance path.
- `Q2 Logic`: complete causal problem -> action -> expected result or decision. A
  diagnostic result may support or refute a hypothesis; it does not prove a root cause
  unless the stated evidence can establish that claim. The plan need not pre-specify
  every implementation detail, but it must show how the result informs the next gate.
- `Q3 Path`: assumptions rechecked; no stacking on an unconfirmed patch, workaround,
  failed plan, or prior agent inference.
- `Q4 Minimum`: the minimum decision-relevant path from current evidence to the next
  safe implementation or acceptance action. A diagnostic step is justified when its
  possible outcomes can materially change that next action; it is not justified merely
  because it is useful, confidence-improving, or completeness-improving.
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

Keep the `TASK_OBJECTIVE` in view: reaching an investigation criterion produces evidence,
not task completion. After each item, decide whether the evidence enables implementation
or acceptance, requires a plan change, or leaves a decision-relevant uncertainty.

An item cannot launch another item. A new local uncertainty may start a new bounded item
only after that return and only if problem, requirements, core assumptions, main strategy,
acceptance, and deliverable remain unchanged. Otherwise return to PLAN; the new item gets
its own three statements. Do not investigate for completeness.

Use a real delegated agent for narrow independent checks when available; never invent
delegation or treat its conclusion as proof. Inspect reproducible evidence and state the
boundary if delegation is unavailable.

### Optional Premise Recheck

During VERIFY, when the current path shows signs of fixation, prefer a context-separated
delegated agent for a narrow premise recheck when one is available. Signs include new
evidence conflicting with the working model, investigation expanding without shrinking
the blocking uncertainty, or accumulating local explanations or patches to maintain the
model.

If delegation is unavailable, perform the same bounded premise recheck explicitly in the
current context and state that context isolation was unavailable. Do not present it as
independent validation.

This is not a new Gate, root-cause investigation, or independent source of truth. It only
challenges upstream assumptions behind the current problem model. Give the recheck agent
the task objective, primary observations/direct evidence, accepted constraints, current
problem model, current path, and blocker; do not give unnecessary persuasive reasoning
from the main agent.

Ask only:

1. Which upstream assumptions does the current path rely on without direct evidence?
2. Which earliest unproven assumption, if false, would invalidate the most downstream
   reasoning?
3. What is the lowest-cost check that could confirm or refute that assumption?
4. If recent derived reasoning is discarded and only primary observations/direct evidence
   are kept, what should be verified first?

The recheck may recommend only `CONTINUE_CURRENT_MODEL`, `BACKTRACK_TO: <assumption>`, or
`RESET_PROBLEM_MODEL`. Its recommendation is not evidence. Return to VERIFY, inspect the
underlying source/log/config/runtime evidence, and make the decision there. Do not let the
recheck create a patch, architecture, or investigation tree.

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
- During VERIFY, a technically decidable finding may refine the verification target or
  invalidate the plan. Do not modify implementation; return to VERIFY or PLAN as required.
- After EXECUTE begins, if preflight, build, or runtime evidence shows a defect introduced
  by the current change, make the smallest technically decidable repair and rerun the
  earliest affected check or stage. If the repair changes the plan materially, return to
  PLAN first; never stack repairs on an unconfirmed assumption.
- Do not escalate an intermediate correction or a choice among valid options; choose the
  smallest compliant option. Escalate only genuine unresolved ambiguity or an external
  decision the evidence cannot settle. Treat user-declared frozen facts as baseline and
  report build/package/validation success only as the evidence it actually supports.
