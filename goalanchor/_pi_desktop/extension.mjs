import { readFile, realpath, stat } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import guard from './guard.cjs';
import compatibility from './compat.cjs';
import bridge from './bridge.cjs';

const root = path.dirname(fileURLToPath(import.meta.url));

// PI-Desktop 0.22.0 rebuilds the agent runtime for every turn, which re-invokes
// this factory, while the module instance itself persists for the whole sidecar
// process (the runner caches the imported factory in a module-level map). Guard
// state therefore lives here, keyed by session id, instead of in the factory
// closure: arming survives across turns and a genuinely new session (new id)
// starts unarmed. There is deliberately no file or other model-writable
// persistence surface holding guard authority.
const MAX_SESSION_STATES = 64;
const sessionStates = new Map();
const instanceId = randomUUID().slice(0, 8);

// PI-Desktop 0.22.0 routes composer-typed slash text to the model as a literal
// prompt (the command palette reaches extension command handlers but fails for
// idle sessions and passes no arguments), so the guard arms and the user grants
// approvals from the raw user prompt in before_agent_start. Arming only
// tightens blocking; prompts can never disarm, and the ui.confirm dialog click
// is the sole grant authority for approvals.
const armedNotice = () =>
  `GoalAnchor runtime guard is armed for this session (instance ${instanceId}): every non-read tool call now requires an exact prior approval and is blocked otherwise. The user grants one-time approvals by typing "/goalanchor-approve <record>.json" in the composer (a human confirmation dialog is shown; without that click nothing is approved). Prompts can never disarm the guard: start a new session to disarm, and re-arm after a plugin reload or host restart (this notice stops when that happens).`;
const guardStateNotice = armed =>
  `GoalAnchor: prompts cannot change the runtime guard (instance ${instanceId}). ${armed ? 'The guard remains armed. To disarm, start a new session or run /goalanchor-guard off from a working command surface.' : 'The guard is not armed. Arm with exactly "/goalanchor-guard on".'}`;
const unknownGuardNotice = () =>
  `GoalAnchor: unrecognized /goalanchor-guard prompt; the runtime guard state is unchanged (instance ${instanceId}). Arm with exactly "/goalanchor-guard on".`;
const noIdentityNotice = () =>
  `GoalAnchor: session identity unavailable; the guard cannot be armed from this prompt (instance ${instanceId}).`;
// Composer approval path: the model or the user may trigger the dialog by
// typing this command; only the human dialog click grants anything.
const APPROVE_HINT =
  ' The user grants approvals by typing "/goalanchor-approve <record>.json" in the composer and confirming the dialog.';
const APPROVE_PREFIX = '/goalanchor-approve ';

function sessionIdOf(ctx) {
  try { return ctx?.sessionManager?.getSessionId?.() ?? null; }
  catch { return null; }
}
function stateFor(sessionId) {
  if (!sessionId) return null;
  let state = sessionStates.get(sessionId);
  if (!state) {
    state = new guard.GuardState();
    sessionStates.set(sessionId, state);
    while (sessionStates.size > MAX_SESSION_STATES) {
      let evicted = false;
      for (const [key, value] of sessionStates) {
        if (!value.enabled) { sessionStates.delete(key); evicted = true; break; }
      }
      if (!evicted) sessionStates.delete(sessionStates.keys().next().value);
    }
  }
  return state;
}
function stateOf(ctx) { return stateFor(sessionIdOf(ctx)); }
function anyArmed() {
  for (const state of sessionStates.values()) if (state.enabled) return true;
  return false;
}

async function readRecord(name, cwd) {
  if (!name || !name.endsWith('.json')) throw new Error('Supply a workspace JSON record path.');
  const base = await realpath(cwd);
  const file = await realpath(path.resolve(base,name));
  const relative = path.relative(base,file);
  if (relative.startsWith('..') || path.isAbsolute(relative)) throw new Error('Record must stay inside the workspace.');
  if ((await stat(file)).size > 1024*1024) throw new Error('Record exceeds 1 MiB.');
  return JSON.parse(await readFile(file,'utf8'));
}

// The preflight result does not echo the raw record, so change_scope and
// recovery_path are read from the parsed record itself (same source as the
// palette handler).
function recordScope(result, parsedRecord) {
  const scope = parsedRecord?.execute?.change_scope;
  const recovery = parsedRecord?.execute?.recovery_path;
  return {scope: typeof scope === 'string' ? scope : '', recovery: typeof recovery === 'string' ? recovery : ''};
}
export default function goalanchor(pi) {
  let python = process.env.GOALANCHOR_PYTHON || 'python';
  const announce = (ctx,message) => { try { ctx.ui?.notify?.(message,'info'); } catch { /* optional UI */ } };
  const requireState = ctx => {
    const state = stateOf(ctx);
    if (!state) throw new Error('Session identity unavailable; cannot resolve guard state.');
    return state;
  };
  pi.registerCommand('goalanchor-status', {
    description:'Show GoalAnchor capabilities and current guard state.',
    handler:async (_args,ctx) => {
      const state = stateOf(ctx);
      announce(ctx,JSON.stringify({
        protocol_version:compatibility.PROTOCOL_VERSION,instance:instanceId,
        guard_enabled:state?.enabled === true,
        remaining_operations:state?.allowed.length ?? 0,
        tool_hook:typeof pi.on === 'function',
        dispatch_mode:'host-completion',python,
      }));
    },
  });
  pi.registerCommand('goalanchor-python', {
    description:'Set the Python executable for this session; pass an unquoted path.',
    handler:async (args,ctx) => {
      const executable = args.trim();
      if (!executable) throw new Error('Supply a Python executable path.');
      stateOf(ctx)?.revoke(); python = executable; announce(ctx,'GoalAnchor Python executable set for this session.');
    },
  });
  pi.registerCommand('goalanchor-guard', {
    description:'Enable or disable explicit tool approval: /goalanchor-guard on|off',
    handler:async (args,ctx) => {
      const state = requireState(ctx);
      if (args.trim() === 'on') {
        if (typeof pi.on !== 'function' || typeof ctx.ui?.confirm !== 'function') {
          throw new Error('Required tool Hook or approval UI is unavailable.');
        }
        state.enable(); announce(ctx,'GoalAnchor guard enabled. Mutations require /goalanchor-approve.');
      } else if (args.trim() === 'off') {
        state.disable(); announce(ctx,'GoalAnchor guard disabled.');
      } else throw new Error('Use /goalanchor-guard on|off.');
    },
  });
  pi.registerCommand('goalanchor-approve', {
    description:'Check PLAN/VERIFY/EXECUTE and approve exact calls from a workspace JSON record.',
    handler:async (args,ctx) => {
      const state = requireState(ctx);
      state.revoke();
      const generation = state.generation;
      if (!state.enabled) throw new Error('Enable /goalanchor-guard on first.');
      if (typeof ctx.ui?.confirm !== 'function') throw new Error('Human confirmation UI unavailable.');
      const model = compatibility.currentModel(ctx);
      const record = await readRecord(args.trim(),ctx.cwd);
      const result = await bridge.runBridge({operation:'preflight',record}, {
        python,signal:ctx.signal,timeoutMs:15000,root,
      });
      if (!result.passed) { announce(ctx,JSON.stringify(result)); return; }
      if (state.generation !== generation) throw new Error('Approval revoked while checking the record.');
      const message = JSON.stringify({goal:result.contract.task_objective,
        constraints:result.contract.constraints,acceptance:result.contract.acceptance_criteria,
        change_scope:record.execute.change_scope,recovery_path:record.execute.recovery_path,
        model,operations:result.operations},null,2);
      if (Buffer.byteLength(message) > 28000) throw new Error('Approval display exceeds 28 KiB; split the operations.');
      if (!await ctx.ui.confirm('GoalAnchor: approve these exact tool calls?',message)) {
        announce(ctx,'GoalAnchor: approval declined.'); return;
      }
      if (ctx.signal?.aborted || compatibility.currentModel(ctx) !== model || state.generation !== generation) {
        throw new Error('Approval context changed or was revoked.');
      }
      state.approve(result.operations,model); announce(ctx,'GoalAnchor: exact operations approved once.');
    },
  });
  pi.registerCommand('goalanchor-check', {
    description:'Validate a preflight JSON record without granting execution permission.',
    handler:async (args,ctx) => {
      const record = await readRecord(args.trim(),ctx.cwd);
      const result = await bridge.runBridge({operation:'preflight',record}, {
        python,signal:ctx.signal,timeoutMs:15000,root,
      });
      announce(ctx,JSON.stringify(result));
    },
  });
  if (typeof pi.on === 'function') {
    pi.on('tool_call', (event,ctx) => {
      const sessionId = sessionIdOf(ctx);
      const state = sessionId ? sessionStates.get(sessionId) : null;
      if (!state?.enabled) {
        if (!sessionId && anyArmed()) {
          return {block:true,reason:'GoalAnchor: session identity unavailable while a guard is armed; blocked.'};
        }
        return;
      }
      try {
        const verdict = state.check(event,compatibility.currentModel(ctx));
        if (verdict?.block && typeof verdict.reason === 'string' && verdict.reason.includes('/goalanchor-approve')) {
          return {...verdict,reason:`${verdict.reason}${APPROVE_HINT}`};
        }
        return verdict;
      }
      catch { state.revoke(); return {block:true,reason:'GoalAnchor: model contract unavailable; blocked.'}; }
    });
    pi.on('tool_result', (event,ctx) => {
      const sessionId = sessionIdOf(ctx);
      const state = sessionId ? sessionStates.get(sessionId) : null;
      state?.result(event);
    });
    pi.on('agent_end', (event,ctx) => {
      const sessionId = sessionIdOf(ctx);
      const state = sessionId ? sessionStates.get(sessionId) : null;
      state?.revoke();
    });
    // The host replaces the run system prompt with the returned value, so the
    // received system prompt is always carried through; the notice is appended.
    // While armed, the notice is re-injected every turn so the model's account
    // of the guard stays grounded and a lapsed arm (reload/restart) is visible
    // by its absence. The approve branch runs BEFORE the armed notice branch so
    // its outcome notice is not clobbered (review gate R2-R2-F1).
    //
    // Composer approval path (review gate R2-R2-F*): the prompt can only
    // trigger the dialog; the ui.confirm click is the sole grant authority.
    // Mirrors the palette handler: generation/model baseline captured before
    // preflight, re-checked after preflight and again after the dialog
    // (R2-R2-F8); whole trimmed remainder is the record path (R2-R2-F5); the
    // 28KiB display cap aborts fail-closed (R2-R2-F4); every branch returns a
    // truthful notice, failures capped and prefixed (R2-R2-F6); the dialog
    // marks prompt-initiated requests (R2-R2-F7). Known host limitation
    // (documented): the whole flow shares the 30s handler budget, so a slow
    // preflight or slow human click fails closed with no notice - visible
    // later as a 'no exact approval' rejection (R2-R2-F2).
    const approveFailureNotice = detail =>
      `GoalAnchor: preflight failed; nothing approved, guard unchanged (instance ${instanceId}). ${detail}`;
    const runApproveBranch = async (event,ctx,text) => {
      const base = typeof event?.systemPrompt === 'string' ? event.systemPrompt : '';
      const record = text.slice(APPROVE_PREFIX.length).trim();
      if (!record) return {systemPrompt:approveFailureNotice('Supply a workspace record path, e.g. "/goalanchor-approve approval.json".').trim()};
      const sessionId = sessionIdOf(ctx);
      const state = stateFor(sessionId);
      if (!state) return {systemPrompt:noIdentityNotice().trim()};
      if (!state.enabled) {
        return {systemPrompt:approveFailureNotice('The guard is not armed; approval requires "/goalanchor-guard on" first. Nothing was approved.').trim()};
      }
      const model = compatibility.currentModel(ctx);
      const generation = state.generation;
      let result; let parsed;
      try {
        parsed = await readRecord(record,ctx.cwd);
        result = await bridge.runBridge({operation:'preflight',record:parsed}, {
          python,signal:ctx.signal,timeoutMs:12000,root,
        });
      } catch (error) {
        const detail = String(error?.message || error).slice(0,400);
        return {systemPrompt:approveFailureNotice(`record/preflight error: ${detail}`).trim()};
      }
      if (state.generation !== generation) {
        return {systemPrompt:approveFailureNotice('approval revoked while checking the record; nothing approved.').trim()};
      }
      if (!result.passed) {
        const detail = JSON.stringify(result).slice(0,2000);
        return {systemPrompt:approveFailureNotice(`preflight result: ${detail}`).trim()};
      }
      const scopeInfo = recordScope(result,parsed);
      const message = JSON.stringify({goal:result.contract.task_objective,
        constraints:result.contract.constraints,acceptance:result.contract.acceptance_criteria,
        change_scope:scopeInfo.scope,recovery_path:scopeInfo.recovery,
        model,operations:result.operations},null,2);
      if (Buffer.byteLength(message) > 28000) {
        return {systemPrompt:approveFailureNotice('approval display exceeds 28 KiB; split the operations. Nothing approved.').trim()};
      }
      let confirmed = false;
      try {
        confirmed = await ctx.ui.confirm('GoalAnchor: approve these exact tool calls? (prompt-initiated request; approve arms nothing)',
          message);
      } catch (error) {
        const detail = String(error?.message || error).slice(0,200);
        return {systemPrompt:approveFailureNotice(`approval dialog unavailable: ${detail}`).trim()};
      }
      if (!confirmed) return {systemPrompt:`GoalAnchor: approval declined; nothing approved (instance ${instanceId}).`.trim()};
      if (ctx.signal?.aborted || compatibility.currentModel(ctx) !== model || state.generation !== generation) {
        return {systemPrompt:approveFailureNotice('approval context changed or was revoked; nothing approved.').trim()};
      }
      state.approve(result.operations,model);
      return {systemPrompt:`GoalAnchor: ${result.operations.length} exact operations approved once by user confirmation (instance ${instanceId}). They are consumed on first matching use; a failed approved call, a model change, or the end of this agent turn revokes the rest. This approval does not change the armed state.`.trim()};
    };
    pi.on('before_agent_start', (event,ctx) => {
      const base = typeof event?.systemPrompt === 'string' ? event.systemPrompt : '';
      const text = typeof event?.prompt === 'string' ? event.prompt.trim() : '';
      let pending = null;
      if (text === '/goalanchor-approve' || text.startsWith(APPROVE_PREFIX)) {
        pending = runApproveBranch(event,ctx,text);
      }
      // The synchronous notice branches run immediately; an async approve
      // branch resolves into the final systemPrompt.
      if (pending) return pending.then(r => r);
      const sessionId = sessionIdOf(ctx);
      let notice = null;
      if (text === '/goalanchor-guard on') {
        const state = stateFor(sessionId);
        if (!state) notice = noIdentityNotice();
        else { state.enable(); notice = armedNotice(); }
      }
      else if (text.startsWith('/goalanchor-guard off')) {
        const state = sessionId ? sessionStates.get(sessionId) : null;
        notice = guardStateNotice(state?.enabled === true);
      }
      else if (text.startsWith('/goalanchor-guard')) notice = unknownGuardNotice();
      else {
        const state = sessionId ? sessionStates.get(sessionId) : null;
        if (state?.enabled) notice = armedNotice();
      }
      if (notice === null) return;
      return {systemPrompt:`${base}\n\n${notice}`.trim()};
    });
  }
}
