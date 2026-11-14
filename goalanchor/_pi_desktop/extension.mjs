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
// idle sessions and passes no arguments), so the guard arms from the raw user
// prompt in before_agent_start. Arming only tightens blocking; prompts can
// never disarm, keeping that decision with the user.
const armedNotice = () =>
  `GoalAnchor runtime guard is armed for this session (instance ${instanceId}): every non-read tool call now requires an exact prior approval and is blocked otherwise. Approvals can only be granted by the user through a working host approval UI; on PI-Desktop 0.22.0 none is available, so treat this session as read-only. Prompts can never disarm the guard: start a new session to disarm, and re-arm after a plugin reload or host restart (this notice stops when that happens).`;
const guardStateNotice = armed =>
  `GoalAnchor: prompts cannot change the runtime guard (instance ${instanceId}). ${armed ? 'The guard remains armed. To disarm, start a new session or run /goalanchor-guard off from a working command surface.' : 'The guard is not armed. Arm with exactly "/goalanchor-guard on".'}`;
const unknownGuardNotice = () =>
  `GoalAnchor: unrecognized /goalanchor-guard prompt; the runtime guard state is unchanged (instance ${instanceId}). Arm with exactly "/goalanchor-guard on".`;
const noIdentityNotice = () =>
  `GoalAnchor: session identity unavailable; the guard cannot be armed from this prompt (instance ${instanceId}).`;
const APPROVE_HINT =
  ' In PI-Desktop, /goalanchor-approve must be run from the command palette; composer text cannot reach it, and on 0.22.0 the palette currently fails for idle sessions.';

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
    // by its absence.
    pi.on('before_agent_start', (event,ctx) => {
      const base = typeof event?.systemPrompt === 'string' ? event.systemPrompt : '';
      const text = typeof event?.prompt === 'string' ? event.prompt.trim() : '';
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
