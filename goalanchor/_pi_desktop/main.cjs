'use strict';
const { runBridge } = require('./bridge.cjs');
const { detectCapabilities, normalizeCompletion } = require('./compat.cjs');
let capability;
let busy = false;
const controllers = new Set();

async function python() {
  const settings = await pi.plugin.getSettings();
  return settings?.python || process.env.GOALANCHOR_PYTHON || 'python';
}
async function review(args, context) {
  if (!capability.review.available) throw new Error(`Review unavailable: ${capability.review.missing.join(', ')}`);
  if (busy) throw new Error('A GoalAnchor review is already running.');
  if (!context?.sessionId) throw new Error('Review requires an active desktop session.');
  busy = true;
  const controller = new AbortController(); controllers.add(controller);
  const cancelled = () => controller.abort();
  context.signal?.addEventListener('abort',cancelled,{once:true});
  if (context.signal?.aborted) controller.abort();
  try {
    const initial = await pi.session.getLlmContext();
    const model = initial?.modelKey;
    if (initial?.sessionId !== context.sessionId || typeof model !== 'string' || !model.includes('/') ||
        context.modelKey && context.modelKey !== model) throw new Error('Current session/model identity mismatch.');
    return await runBridge({operation:'review', request:args.request, model}, {
      python:await python(), signal:controller.signal,
      complete:async message => {
        if (controller.signal.aborted) throw new Error('Review cancelled.');
        const current = await pi.session.getLlmContext();
        if (current?.sessionId !== initial.sessionId || current?.modelKey !== model) {
          throw new Error('Session/model changed during review.');
        }
        const response = await pi.agent.complete({modelKey:model,
          ...(initial.thinkingLevel ? {thinkingLevel:initial.thinkingLevel} : {}),
          messages:[{role:'user',content:message.prompt}], includeSessionContext:false});
        if (controller.signal.aborted) throw new Error('Review cancelled; late result discarded.');
        const after = await pi.session.getLlmContext();
        if (after?.sessionId !== initial.sessionId || after?.modelKey !== model) {
          throw new Error('Session/model changed before the review response was accepted.');
        }
        return normalizeCompletion(response,model);
      },
    });
  } finally {
    busy = false; controllers.delete(controller);
    context.signal?.removeEventListener('abort',cancelled);
  }
}
async function gate(args, context) {
  const controller = new AbortController(); controllers.add(controller);
  const cancelled = () => controller.abort();
  context?.signal?.addEventListener('abort',cancelled,{once:true});
  if (context?.signal?.aborted) controller.abort();
  try {
    return await runBridge({operation:'gate',phase:args.phase,record:args.record},
      {python:await python(), signal:controller.signal, timeoutMs:15000});
  } finally {
    controllers.delete(controller); context?.signal?.removeEventListener('abort',cancelled);
  }
}
async function onLoad() {
  capability = await detectCapabilities(pi);
  if (typeof pi.commands?.register === 'function') {
    await pi.commands.register({id:'goalanchor.capabilities',title:'GoalAnchor: Compatibility report',
      run:async () => {
        capability = await detectCapabilities(pi);
        if (typeof pi.ui?.showToast === 'function') await pi.ui.showToast(JSON.stringify(capability),'info');
        return capability;
      }});
  }
  if (!capability.review.available) {
    if (typeof pi.ui?.showToast === 'function') {
      await pi.ui.showToast(`GoalAnchor: review unavailable (${capability.review.missing.join(', ')})`,'warn');
    }
  }
  if (typeof pi.agent?.registerTool !== 'function') return;
  await pi.agent.registerTool({name:'goalanchor_gate',description:'Validate GoalAnchor records; no execution grant.',
    risk:'low', schema:{type:'object',properties:{phase:{type:'string'},record:{type:'object'}},
      required:['phase','record']},execute:gate});
  if (capability.review.available) {
    await pi.agent.registerTool({name:'goalanchor_review',description:'Independent bounded review on the current model.',
      risk:'medium',schema:{type:'object',properties:{request:{type:'object'}},required:['request']},execute:review});
  }
}
async function onUnload() {
  for (const controller of controllers) controller.abort();
  controllers.clear();
  if (typeof pi.commands?.unregister === 'function') await pi.commands.unregister('goalanchor.capabilities');
  if (typeof pi.agent?.unregisterTool === 'function') {
    await pi.agent.unregisterTool('goalanchor_gate');
    await pi.agent.unregisterTool('goalanchor_review');
  }
}
module.exports = { onLoad, onUnload, review, gate };
