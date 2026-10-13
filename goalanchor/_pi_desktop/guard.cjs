'use strict';
const { createHash } = require('node:crypto');
const READ_TOOLS = new Set(['Read', 'Glob', 'Grep', 'ToolSearch']);
const CHECK_TOOLS = new Set(['goalanchor_gate', 'goalanchor_review',
  'plugin_local_goalanchor_goalanchor_gate', 'plugin_local_goalanchor_goalanchor_review']);

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') {
    const out = Object.create(null);
    for (const key of Object.keys(value).sort()) out[key] = canonical(value[key]);
    return out;
  }
  if (value === undefined || typeof value === 'function' || typeof value === 'symbol' ||
      typeof value === 'number' && !Number.isFinite(value)) throw new Error('Input is not JSON.');
  return value;
}
function signature(tool, input) {
  if (typeof tool !== 'string' || !tool.trim() || !input || typeof input !== 'object' || Array.isArray(input)) {
    throw new Error('An exact tool name and input object are required.');
  }
  const value = JSON.stringify(canonical({tool,input}));
  if (Buffer.byteLength(value) > 32768) throw new Error('Operation exceeds 32 KiB.');
  return createHash('sha256').update(value).digest('hex');
}
class GuardState {
  constructor() { this.enabled = false; this.revoke(); }
  revoke() {
    this.generation = (this.generation || 0) + 1;
    this.allowed = []; this.pending = new Set(); this.model = null;
  }
  enable() { this.revoke(); this.enabled = true; }
  disable() { this.revoke(); this.enabled = false; }
  approve(operations, model) {
    this.revoke();
    if (!this.enabled || typeof model !== 'string' || !model.includes('/')) throw new Error('Guard/model unavailable.');
    if (!Array.isArray(operations) || !operations.length || operations.length > 32) throw new Error('Invalid operations.');
    const values = operations.map(op => {
      if (CHECK_TOOLS.has(op.tool) || op.tool?.startsWith('goalanchor_')) throw new Error('Cannot authorize guard controls.');
      return signature(op.tool, op.input);
    });
    this.allowed = values; this.model = model;
  }
  check(event, model) {
    if (!this.enabled) return;
    try {
      if (this.model !== null && model !== this.model) {
        this.revoke(); return {block:true, reason:'GoalAnchor: model changed; approval revoked.'};
      }
      if (READ_TOOLS.has(event.toolName) || CHECK_TOOLS.has(event.toolName)) return;
      if (typeof event.toolCallId !== 'string' || !event.toolCallId) throw new Error('Missing tool call identity.');
      const key = signature(event.toolName,event.input);
      const at = this.allowed.indexOf(key);
      if (at < 0) {
        this.revoke(); return {block:true, reason:'GoalAnchor: no exact approval; use /goalanchor-approve.'};
      }
      this.allowed.splice(at,1); this.pending.add(event.toolCallId);
    } catch {
      this.revoke(); return {block:true, reason:'GoalAnchor: invalid tool contract; approval revoked.'};
    }
  }
  result(event) {
    if (this.pending.delete(event.toolCallId) && event.isError) this.revoke();
  }
}
module.exports = { GuardState, signature };
