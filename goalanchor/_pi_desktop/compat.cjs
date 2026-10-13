'use strict';

const PROTOCOL_VERSION = 1;
function has(host, group, member) { return typeof host?.[group]?.[member] === 'function'; }
async function detectCapabilities(host) {
  let version = 'unknown';
  if (has(host, 'app', 'getVersion')) {
    try { version = String(await host.app.getVersion()); } catch { /* optional diagnostic */ }
  }
  const missing = [['agent','complete'], ['agent','registerTool'], ['agent','unregisterTool'],
    ['session','getLlmContext']].filter(([g,m]) => !has(host,g,m)).map(([g,m]) => `${g}.${m}`);
  const parts = /^(\d+)\.(\d+)\.(\d+)/.exec(version);
  if (parts && Number(parts[1]) === 0 && Number(parts[2]) < 18) missing.push('host >= 0.18.0');
  return {
    version, protocol_version: PROTOCOL_VERSION, minimum_version: '0.18.0',
    review: { available: !missing.length, missing, dispatch_mode: 'host-completion' },
    visible_subagents: { available: false, reason: 'No verified public child-session dispatch contract.' },
    remote_cancel: { available: false, reason: 'No verified cancellation parameter on agent.complete.' },
    status: { available: has(host,'ui','showToast') },
    review_budget_ms:100000, review_modes:['agent'], tested_protocol_baseline:'0.18.0',
  };
}
function normalizeCompletion(result, model) {
  if (!result || typeof result !== 'object' || result.modelKey !== model) {
    throw new Error('Completion model identity is missing or differs from the session model.');
  }
  if (typeof result.text !== 'string' || !result.text.trim() || Buffer.byteLength(result.text) > 1024*1024) {
    throw new Error('Completion text is empty or exceeds 1 MiB.');
  }
  return { text: result.text, modelKey: result.modelKey, usage: result.usage };
}
function currentModel(context) {
  const model = context?.model;
  if (typeof model?.provider !== 'string' || typeof model?.id !== 'string' || !model.provider || !model.id) {
    throw new Error('Current provider/model identity is unavailable.');
  }
  return `${model.provider}/${model.id}`;
}
module.exports = { PROTOCOL_VERSION, detectCapabilities, normalizeCompletion, currentModel };
