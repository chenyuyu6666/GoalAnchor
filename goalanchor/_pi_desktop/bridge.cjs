'use strict';
const { spawn } = require('node:child_process');
const path = require('node:path');
const { PROTOCOL_VERSION } = require('./compat.cjs');
const MAX_BYTES = 1024 * 1024;

function runBridge(payload, { python = 'python', signal, complete, timeoutMs = 100000, root = __dirname } = {}) {
  return new Promise((resolve,reject) => {
    if (signal?.aborted) { reject(new Error('GoalAnchor cancelled.')); return; }
    if (typeof python !== 'string' || !python.trim()) { reject(new Error('Python executable missing.')); return; }
    let encoded;
    try { encoded = JSON.stringify({...payload, protocol_version: PROTOCOL_VERSION}) + '\n'; }
    catch (error) { reject(error); return; }
    if (Buffer.byteLength(encoded) > MAX_BYTES) { reject(new Error('Bridge request exceeds 1 MiB.')); return; }
    const child = spawn(python, ['-I', '-u', path.join(root,'runtime','bridge.py')],
      {shell:false, windowsHide:true, stdio:['pipe','pipe','pipe']});
    let buffer = ''; let result; let settled = false; let diagnostic = ''; let total = 0;
    const ids = new Set();
    function finish(error, value) {
      if (settled) return;
      settled = true; clearTimeout(timer); signal?.removeEventListener('abort',cancel);
      child.stdin.destroy(); child.kill();
      if (error) reject(error); else resolve(value);
    }
    const cancel = () => finish(new Error('GoalAnchor cancelled; late results discarded.'));
    const timer = setTimeout(() => finish(new Error('GoalAnchor bridge timeout.')),timeoutMs);
    signal?.addEventListener('abort',cancel,{once:true});
    child.on('error',error => finish(new Error(`Python bridge unavailable: ${error.message}`)));
    child.stdin.on('error',error => { if (!settled) finish(error); });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data',chunk => { diagnostic = (diagnostic + chunk).slice(-2000); });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data',chunk => {
      if (settled) return;
      total += Buffer.byteLength(chunk); buffer += chunk;
      if (total > MAX_BYTES * 8 || Buffer.byteLength(buffer) > MAX_BYTES) {
        finish(new Error('Bridge output limit exceeded.')); return;
      }
      let end;
      while (!settled && (end = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0,end); buffer = buffer.slice(end+1);
        let message;
        try { message = JSON.parse(line); } catch { finish(new Error('Bridge returned invalid JSON.')); return; }
        if (!message || typeof message !== 'object' || Array.isArray(message)) {
          finish(new Error('Bridge message must be a JSON object.')); return;
        }
        if (message.type === 'agent_request') {
          if (typeof complete !== 'function' || typeof message.id !== 'string' || ids.has(message.id) ||
              typeof message.prompt !== 'string' || message.prompt.length > 200000) {
            finish(new Error('Invalid bridge completion request.')); return;
          }
          ids.add(message.id);
          Promise.resolve().then(() => { if (settled) throw new Error('Cancelled.'); return complete(message); })
            .then(response => {
              if (settled) return;
              try {
                const encoded = JSON.stringify({type:'agent_response',id:message.id,response})+'\n';
                if (Buffer.byteLength(encoded) > MAX_BYTES) throw new Error('Completion response exceeds 1 MiB.');
                child.stdin.write(encoded);
              } catch (error) { finish(error); }
            },error => {
              if (!settled) child.stdin.write(JSON.stringify({type:'agent_response',id:message.id,
                error:String(error?.message || error)})+'\n');
            });
        } else if (message.type === 'result' && message.protocol_version === PROTOCOL_VERSION && result === undefined) {
          result = message.result;
          child.stdin.end();
        } else if (message.type === 'error') finish(new Error(String(message.error)));
        else finish(new Error('Unsupported bridge output protocol.'));
      }
    });
    child.on('close',code => {
      if (settled) return;
      if (code !== 0 || result === undefined || buffer.trim()) {
        finish(new Error(`Bridge failed (${code}): ${diagnostic || 'incomplete result'}`));
      } else finish(null,result);
    });
    child.stdin.write(encoded);
    if (payload.operation !== 'review') child.stdin.end();
  });
}
module.exports = { runBridge };
