import { readFile, realpath, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import guard from './guard.cjs';
import compatibility from './compat.cjs';
import bridge from './bridge.cjs';

const root = path.dirname(fileURLToPath(import.meta.url));

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
  // State is factory-local: cached module instances cannot share approvals between sessions.
  const state = new guard.GuardState();
  let python = process.env.GOALANCHOR_PYTHON || 'python';
  const announce = (ctx,message) => { try { ctx.ui?.notify?.(message,'info'); } catch { /* optional UI */ } };
  pi.registerCommand('goalanchor-status', {
    description:'Show GoalAnchor capabilities and current guard state.',
    handler:async (_args,ctx) => announce(ctx,JSON.stringify({
      protocol_version:compatibility.PROTOCOL_VERSION,guard_enabled:state.enabled,
      remaining_operations:state.allowed.length,tool_hook:typeof pi.on === 'function',
      dispatch_mode:'host-completion',python,
    })),
  });
  pi.registerCommand('goalanchor-python', {
    description:'Set the Python executable for this session; pass an unquoted path.',
    handler:async (args,ctx) => {
      const executable = args.trim();
      if (!executable) throw new Error('Supply a Python executable path.');
      state.revoke(); python = executable; announce(ctx,'GoalAnchor Python executable set for this session.');
    },
  });
  pi.registerCommand('goalanchor-guard', {
    description:'Enable or disable explicit tool approval: /goalanchor-guard on|off',
    handler:async (args,ctx) => {
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
    pi.on('session_start', () => state.disable());
    pi.on('session_shutdown', () => state.disable());
    pi.on('tool_call', (event,ctx) => {
      if (!state.enabled) return;
      try { return state.check(event,compatibility.currentModel(ctx)); }
      catch { state.revoke(); return {block:true,reason:'GoalAnchor: model contract unavailable; blocked.'}; }
    });
    pi.on('tool_result', event => state.result(event));
    pi.on('agent_end', () => state.revoke());
  }
}
