const test = require('node:test');
const assert = require('node:assert/strict');
const { detectCapabilities, normalizeCompletion } = require('../../goalanchor/_pi_desktop/compat.cjs');
const { GuardState } = require('../../goalanchor/_pi_desktop/guard.cjs');

test('0.18 and future hosts use capabilities, tolerate extra fields', async () => {
  for (const version of ['0.18.0', '0.19.1', '9.0.0']) {
    const host = { app: { getVersion: async () => version },
      agent: { complete() {}, registerTool() {}, unregisterTool() {} },
      session: { getLlmContext() {} }, futureApi() {} };
    const report = await detectCapabilities(host);
    assert.equal(report.review.available, true);
    assert.equal(report.version, version);
    assert.equal(report.visible_subagents.available, false);
  }
  assert.equal(normalizeCompletion({text:'{}', modelKey:'p/m', future:true}, 'p/m').text, '{}');
});

test('missing required APIs and mismatched model never pass', async () => {
  assert.equal((await detectCapabilities({})).review.available, false);
  const old = {app:{getVersion:async () => '0.17.9'},agent:{complete(){},registerTool(){},unregisterTool(){}},
    session:{getLlmContext(){}}};
  assert.equal((await detectCapabilities(old)).review.available,false);
  assert.throws(() => normalizeCompletion({text:'{}', modelKey:'p/other'}, 'p/m'), /model/i);
  assert.throws(() => normalizeCompletion({text:'{}'}, 'p/m'), /model/i);
  assert.throws(() => normalizeCompletion({text:'', modelKey:'p/m'}, 'p/m'));
});

test('guard is opt-in, exact, single-use, isolated and fails closed', () => {
  const a = new GuardState(); const b = new GuardState();
  const call = {toolName:'edit', input:{path:'中文.py', text:'fix'}, toolCallId:'1'};
  assert.equal(a.check(call, 'p/m'), undefined);
  a.enable();
  assert.equal(a.check(call, 'p/m').block, true);
  a.approve([{tool:'edit', input:call.input}], 'p/m');
  assert.equal(a.check(call, 'p/m'), undefined);
  assert.equal(a.check(call, 'p/m').block, true);
  assert.equal(b.check(call, 'p/m'), undefined);
  a.approve([{tool:'edit', input:call.input}], 'p/m');
  assert.equal(a.check({...call, input:{...call.input, text:'different'}}, 'p/m').block, true);
  assert.equal(a.check(call, 'p/m').block, true);
});

test('model changes and tool errors revoke remaining operations', () => {
  const g = new GuardState(); g.enable();
  const ops = [{tool:'bash',input:{command:'first'}},{tool:'bash',input:{command:'second'}}];
  g.approve(ops,'p/m');
  assert.equal(g.check({toolName:'bash',input:ops[0].input,toolCallId:'1'},'p/new').block,true);
  g.approve(ops,'p/m');
  assert.equal(g.check({toolName:'bash',input:ops[0].input,toolCallId:'1'},'p/m'),undefined);
  g.result({toolCallId:'1',isError:true});
  assert.equal(g.check({toolName:'bash',input:ops[1].input,toolCallId:'2'},'p/m').block,true);
});

test('approval cannot authorize guard controls or malformed inputs', () => {
  const g = new GuardState(); g.enable();
  assert.throws(() => g.approve([{tool:'goalanchor_review',input:{}}], 'p/m'));
  assert.throws(() => g.approve([{tool:'edit',input:null}], 'p/m'));
  assert.throws(() => g.approve([{tool:'edit',input:{}}], ''));
  assert.throws(() => g.approve([{tool:'plugin_local_goalanchor_goalanchor_review',input:{}}], 'p/m'));
});

test('actual desktop namespaced check tools remain available under guard', () => {
  const g = new GuardState(); g.enable();
  for (const toolName of ['plugin_local_goalanchor_goalanchor_review','plugin_local_goalanchor_goalanchor_gate']) {
    assert.equal(g.check({toolName,input:{},toolCallId:'x'},'p/m'),undefined);
  }
  assert.equal(g.check({toolName:'plugin_other_goalanchor_gate',input:{},toolCallId:'x'},'p/m').block,true);
});

test('desktop ToolSearch discovery stays available without approving writes', () => {
  const g = new GuardState(); g.enable();
  assert.equal(g.check({toolName:'ToolSearch',input:{query:'write'},toolCallId:'discover'},'p/m'),undefined);
  assert.equal(g.check({toolName:'write',input:{path:'demo',content:'x'},toolCallId:'write'},'p/m').block,true);
});

test('desktop core read tools use exact case and do not allow mutation aliases', () => {
  const g = new GuardState(); g.enable();
  for (const toolName of ['Read','Glob','Grep']) {
    assert.equal(g.check({toolName,input:{path:'demo'},toolCallId:toolName},'p/m'),undefined);
  }
  for (const toolName of ['Write','Edit','Bash','read','plugin_other_Read']) {
    assert.equal(g.check({toolName,input:{path:'demo'},toolCallId:toolName},'p/m').block,true);
  }
});

const { runBridge } = require('../../goalanchor/_pi_desktop/bridge.cjs');
const path = require('node:path');
const os = require('node:os');
const fs = require('node:fs/promises');
const python = process.env.GOALANCHOR_TEST_PYTHON || 'python';
let plugin = process.env.GOALANCHOR_TEST_PLUGIN;
let generated;
if (!plugin) {
  const syncfs = require('node:fs');
  generated = syncfs.mkdtempSync(path.join(os.tmpdir(),'goalanchor-node-test-'));
  plugin = path.join(generated,'plugin');
  require('node:child_process').execFileSync(python,['-m','goalanchor.pi_desktop','build','--destination',plugin],
    {cwd:path.resolve(__dirname,'../..'),windowsHide:true,stdio:'pipe'});
}
test.after(async () => {
  if (generated && path.dirname(path.resolve(generated)) === path.resolve(os.tmpdir())) {
    await fs.rm(generated,{recursive:true,force:true});
  }
});

test('portable bridge runs full host completion review and dispute', {skip:!plugin}, async () => {
  const calls = [];
  const result = await runBridge({operation:'review',model:'p/m',request:{risk:'HIGH',max_rounds:2,
    context:{goal:'fix',prompt:'review',constraints:['no regression'],evidence_refs:['E1'],candidate:'patch'}}},
    {python,root:plugin,complete:async message => {
      const request = message.request; calls.push(request);
      const base = {round_number:request.round_number,context_fingerprint:request.context.fingerprint,verdict:'PASS',future:42};
      let result;
      if (request.role === 'reviewer') result = {...base,reviewer_id:request.agent_id,isolated:true,findings:[]};
      else if (request.role === 'arbiter') result = {...base,arbiter_id:request.agent_id,summary:'reviewed',rationale:'checked',
        reviewer_ids:request.reviewer_results.map(r => r.reviewer_id),disagrees_with_reviewers:request.round_number===1};
      else result = {...base,reviewer_id:request.agent_id,arbiter_id:request.arbiter_result.arbiter_id,
        summary:'meta checked',findings:[],isolated:true,agrees_with_arbiter:true};
      return {text:JSON.stringify(result),modelKey:'p/m'};
    }});
  assert.equal(result.outcome,'PASS'); assert.equal(result.round_number,2); assert.equal(calls.length,8);
  assert.equal(result.agent_results.length,8);
  assert.equal(new Set(calls.map(c => c.context.fingerprint)).size,1);
});

test('bridge rejects missing Python, invalid output, timeout, cancellation and oversize input', async () => {
  await assert.rejects(runBridge({operation:'gate'},{python:'not-a-real-goalanchor-python'}),/unavailable/i);
  await assert.rejects(runBridge({record:'x'.repeat(1024*1024)}),/exceeds/i);
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-bridge-test-'));
  try {
    await fs.mkdir(path.join(directory,'runtime'));
    const file = path.join(directory,'runtime','bridge.py');
    await fs.writeFile(file,"print('not json',flush=True)\n");
    await assert.rejects(runBridge({}, {python,root:directory}),/JSON/i);
    for (const value of ['null','[]','42','"string"']) {
      await fs.writeFile(file,`print('${value}',flush=True)\n`);
      await assert.rejects(runBridge({}, {python,root:directory}),/object/i);
    }
    await fs.writeFile(file,'import time\ntime.sleep(10)\n');
    await assert.rejects(runBridge({}, {python,root:directory,timeoutMs:40}),/timeout/i);
    const controller = new AbortController();
    const pending = runBridge({}, {python,root:directory,signal:controller.signal});
    controller.abort(); await assert.rejects(pending,/cancel/i);
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});

test('plugin loads on baseline/newer protocol and rejects changed session/model', {skip:!plugin}, async () => {
  const modulePath = path.join(plugin,'main.cjs');
  for (const version of ['0.18.0','0.19.1']) {
    delete require.cache[require.resolve(modulePath)];
    const tools = new Map(); const commands = new Map(); const completions = [];
    let model = 'p/m';
    global.pi = {
      app:{getVersion:async () => version},plugin:{getSettings:async () => ({python})},
      session:{getLlmContext:async () => ({sessionId:'S1',modelKey:model,thinkingLevel:'off',future:true})},
      commands:{register:async definition => commands.set(definition.id,definition),
        unregister:async id => commands.delete(id)},
      agent:{registerTool:async definition => tools.set(definition.name,definition),
        unregisterTool:async name => tools.delete(name),complete:async input => {
          completions.push(input); model='p/changed'; return {text:'{}',modelKey:'p/m'};
        }},
    };
    const app = require(modulePath);
    await app.onLoad();
    assert.equal((await commands.get('goalanchor.capabilities').run()).version,version);
    assert.equal(tools.size,2);
    await assert.rejects(tools.get('goalanchor_review').execute({request:{risk:'LOW',max_rounds:1,
      context:{goal:'fix',prompt:'review',constraints:[],evidence_refs:['E1'],candidate:'patch'}}},
      {sessionId:'S1',modelKey:'p/m',signal:new AbortController().signal}),/changed/);
    assert.equal(completions[0].includeSessionContext,false);
    assert.equal(completions[0].modelKey,'p/m');
    assert.equal(completions[0].messages.length,1);
    await app.onUnload(); assert.equal(tools.size,0); assert.equal(commands.size,0);
    delete global.pi;
  }
});

test('plugin cancellation aborts local bridge and ignores late completion', {skip:!plugin}, async () => {
  const modulePath = path.join(plugin,'main.cjs'); delete require.cache[require.resolve(modulePath)];
  let markStarted; const started = new Promise(resolve => {markStarted=resolve;});
  let finishCompletion;
  global.pi = {app:{getVersion:async () => '0.18.0'},plugin:{getSettings:async () => ({python})},
    session:{getLlmContext:async () => ({sessionId:'S1',modelKey:'p/m'})},
    agent:{registerTool:async () => {},unregisterTool:async () => {},complete:async () => {
      markStarted(); return new Promise(resolve => {finishCompletion=resolve;});
    }}};
  const app = require(modulePath); await app.onLoad();
  const controller = new AbortController();
  const pending = app.review({request:{risk:'LOW',max_rounds:1,
    context:{goal:'fix',prompt:'review',constraints:[],evidence_refs:['E1'],candidate:'patch'}}},
    {sessionId:'S1',modelKey:'p/m',signal:controller.signal});
  const check = assert.rejects(pending,/cancel/);
  await started; controller.abort(); await check;
  finishCompletion({text:'{}',modelKey:'p/m'}); await new Promise(resolve => setImmediate(resolve));
  await app.onUnload(); delete global.pi;
});

test('extension user command and Hook enforce confirmed exact approval', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const fixture = JSON.parse(await fs.readFile(path.join(plugin,'examples','approval.json'),'utf8'));
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-extension-test-'));
  const session = id => ({sessionManager:{getSessionId:() => id}});
  try {
    await fs.writeFile(path.join(directory,'approval.json'),JSON.stringify(fixture));
    const commands = new Map(); const hooks = new Map();
    create({registerCommand:(name,definition) => commands.set(name,definition),on:(name,handler) => hooks.set(name,handler)});
    const messages=[]; let confirmed=false;
    const context={cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      ...session('S1'),
      ui:{notify:message => messages.push(message),confirm:async () => confirmed}};
    await commands.get('goalanchor-python').handler(python,context);
    await commands.get('goalanchor-guard').handler('on',context);
    const op=fixture.operations[0]; const call={toolName:op.tool,input:op.input,toolCallId:'1'};
    assert.equal(hooks.get('tool_call')(call,context).block,true);
    await commands.get('goalanchor-approve').handler('approval.json',context);
    assert.equal(hooks.get('tool_call')(call,context).block,true);
    confirmed=true;
    await commands.get('goalanchor-approve').handler('approval.json',context);
    assert.equal(hooks.get('tool_call')(call,context),undefined);
    assert.equal(hooks.get('tool_call')(call,context).block,true);
    await commands.get('goalanchor-approve').handler('approval.json',context);
    hooks.get('agent_end')({},context);
    assert.equal(hooks.get('tool_call')(call,context).block,true);
    let show; const shown = new Promise(resolve => {show=resolve;});
    let finish;
    context.ui.confirm = () => { show(); return new Promise(resolve => {finish=resolve;}); };
    const stale = commands.get('goalanchor-approve').handler('approval.json',context);
    await shown; hooks.get('agent_end')({},context); finish(true);
    await assert.rejects(stale,/changed|revoked/i);
    assert.equal(hooks.get('tool_call')(call,context).block,true);
    hooks.get('tool_result')({toolCallId:'none'},context);
    await assert.rejects(commands.get('goalanchor-guard').handler('on',{...context,sessionManager:{getSessionId:() => null}}),/identity/i);
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});

test('before_agent_start arms the guard from the exact prompt and never disarms', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const commands = new Map(); const hooks = new Map();
  create({registerCommand:(name,definition) => commands.set(name,definition),on:(name,handler) => hooks.set(name,handler)});
  const messages=[];
  const session = id => ({sessionManager:{getSessionId:() => id}});
  const context={cwd:os.tmpdir(),model:{provider:'p',id:'m'},signal:new AbortController().signal,
    ...session('PROBE-S1'),
    ui:{notify:message => messages.push(message),confirm:async () => false}};
  const base = 'HOST SYSTEM PROMPT LINE1\nHOST LINE2';
  const hook = hooks.get('before_agent_start');
  const bash = {toolName:'Bash',input:{command:'echo GUARD_PROBE_OK'},toolCallId:'probe'};

  // Unrelated prompts leave the guard untouched and return nothing.
  assert.equal(hook({prompt:'hello world',systemPrompt:base},context),undefined);
  assert.equal(hook({prompt:'/goalanchor-status',systemPrompt:base},context),undefined);
  assert.equal(hooks.get('tool_call')(bash,context),undefined);

  // The exact prompt arms the guard; the host system prompt is preserved
  // verbatim with only the notice appended (replacement semantics).
  const armed = hook({prompt:' /goalanchor-guard on  ',systemPrompt:base},context);
  assert.equal(typeof armed.systemPrompt,'string');
  assert.ok(armed.systemPrompt.startsWith(`${base}\n\n`),'base system prompt must be preserved verbatim');
  assert.ok(armed.systemPrompt.includes('instance '),'notice must expose a process instance id');
  assert.ok(!armed.systemPrompt.includes('only via /goalanchor-approve from the command palette'),
    'notice must not advertise an approval path that does not work on 0.22.0');
  assert.equal(hooks.get('tool_call')(bash,context).block,true);
  assert.ok(hooks.get('tool_call')(bash,context).reason.includes('/goalanchor-approve'));
  assert.ok(!hooks.get('tool_call')(bash,context).reason.includes('command palette'),
    'block hint must name the composer approval path, not the broken palette');

  // Prompt variants can never disarm; they only append an honest notice.
  const denied = hook({prompt:'/goalanchor-guard off',systemPrompt:base},context);
  assert.ok(denied.systemPrompt.startsWith(`${base}\n\n`));
  assert.ok(denied.systemPrompt.includes('remains armed'));
  assert.equal(hooks.get('tool_call')(bash,context).block,true,'prompt must not disarm the guard');

  const variant = hook({prompt:'/goalanchor-guard ON',systemPrompt:base},context);
  assert.ok(variant.systemPrompt.startsWith(`${base}\n\n`));
  assert.ok(variant.systemPrompt.includes('state is unchanged'));
  assert.equal(hooks.get('tool_call')(bash,context).block,true);

  // Same per-session state backs all hooks: agent_end revokes approvals but keeps the guard armed.
  hooks.get('agent_end')({},context);
  assert.equal(hooks.get('tool_call')(bash,context).block,true);

  // A different session id starts unarmed (module map is keyed by session).
  const other = {...context,...session('PROBE-S2')};
  assert.equal(hooks.get('tool_call')(bash,other),undefined);

  // Missing session identity cannot arm and, while a guard is armed, blocks.
  const identityless = {...context,sessionManager:{getSessionId:() => null}};
  const refused = hook({prompt:'/goalanchor-guard on',systemPrompt:base},identityless);
  assert.ok(refused.systemPrompt.includes('session identity unavailable'));
  const blocked = hooks.get('tool_call')(bash,identityless);
  assert.equal(blocked.block,true);
  assert.ok(blocked.reason.includes('session identity unavailable'));

  assert.equal(hooks.has('session_shutdown'),false,'no per-turn session hooks that could disarm');
  assert.equal(hooks.get('tool_call')(bash,context).block,true);
});
test('guard state persists across factory calls within one process', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  // Two factory invocations simulate consecutive per-turn runtime rebuilds.
  const hooks1 = new Map(); const hooks2 = new Map();
  create({registerCommand:() => {},on:(name,handler) => hooks1.set(name,handler)});
  create({registerCommand:() => {},on:(name,handler) => hooks2.set(name,handler)});
  const session = {sessionManager:{getSessionId:() => 'PERSIST-S1'}};
  const context={model:{provider:'p',id:'m'},signal:new AbortController().signal,...session,
    ui:{notify:() => {},confirm:async () => false}};
  const base = 'BASE';
  const armed = hooks1.get('before_agent_start')({prompt:'/goalanchor-guard on',systemPrompt:base},context);
  assert.ok(armed.systemPrompt.startsWith(`${base}\n\n`));
  const bash = {toolName:'Bash',input:{command:'echo next-turn'},toolCallId:'t1'};
  assert.equal(hooks2.get('tool_call')(bash,context).block,true,
    'arming from one factory call must hold in the next factory call (per-turn runtime rebuild)');
});

test('notices never advertise the broken palette path and carry the instance id', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const hooks = new Map();
  create({registerCommand:() => {},on:(name,handler) => hooks.set(name,handler)});
  const context={model:{provider:'p',id:'m'},signal:new AbortController().signal,
    sessionManager:{getSessionId:() => 'NOTICE-S1'},ui:{notify:() => {},confirm:async () => false}};
  const hook = hooks.get('before_agent_start');
  const out = hook({prompt:'/goalanchor-guard on',systemPrompt:'B'},context);
  assert.ok(out.systemPrompt.includes('instance '));
  assert.ok(out.systemPrompt.includes('/goalanchor-approve'), 'armed notice must name the composer approval path');
  assert.ok(!out.systemPrompt.includes('command palette'), 'no notice may point at the broken palette');
  const bash = {toolName:'Bash',input:{command:'x'},toolCallId:'n1'};
  const blocked = hooks.get('tool_call')(bash,context);
  assert.ok(blocked.reason.includes('/goalanchor-approve'));
  assert.ok(!blocked.reason.includes('command palette'), 'block hint must name the composer path');
});

test('composer approve branch: dialog click grants once, prompts never grant without it', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const fixture = JSON.parse(await fs.readFile(path.join(plugin,'examples','approval.json'),'utf8'));
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-approve-test-'));
  try {
    await fs.writeFile(path.join(directory,'approval.json'),JSON.stringify(fixture));
    const hooks = new Map();
    create({registerCommand:() => {},on:(name,handler) => hooks.set(name,handler)});
    const bash = {toolName:'Bash',input:{command:'echo approved-once'},toolCallId:'a1'};
    const session = id => ({sessionManager:{getSessionId:() => id}});
    let confirmResult = false; let dialogCalls = 0; let dialogTitle=''; let dialogBody='';
    const mkContext = () => ({cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      ...session('APPR-S1'),
      ui:{notify:() => {},confirm:async (title,body) => {dialogCalls++;dialogTitle=title;dialogBody=body;return confirmResult;}}});
    const hook = hooks.get('before_agent_start');
    const context = mkContext();

    // Approve while unarmed: honest failure, no dialog.
    let out = await hook({prompt:'/goalanchor-approve approval.json',systemPrompt:'B'},context);
    assert.ok(out.systemPrompt.includes('not armed'));
    assert.equal(dialogCalls,0);

    // Arm via the exact prompt, then decline in the dialog: nothing granted.
    hook({prompt:'/goalanchor-guard on',systemPrompt:'B'},context);
    confirmResult = false;
    out = await hook({prompt:'/goalanchor-approve approval.json',systemPrompt:'B'},context);
    assert.equal(dialogCalls,1);
    assert.ok(out.systemPrompt.includes('declined'));
    assert.ok(dialogTitle.includes('prompt-initiated'), 'dialog must mark prompt-initiated requests');
    assert.equal(hooks.get('tool_call')(bash,context).block,true);

    // Confirm in the dialog: exactly the fixture operations are granted once.
    confirmResult = true;
    out = await hook({prompt:'/goalanchor-approve approval.json',systemPrompt:'B'},context);
    assert.equal(dialogCalls,2);
    assert.ok(out.systemPrompt.includes('approved once'));
    assert.ok(dialogBody.includes(fixture.operations[0].tool));
    const op = fixture.operations[0];
    const approvedCall = {toolName:op.tool,input:op.input,toolCallId:'a2'};
    assert.equal(hooks.get('tool_call')(approvedCall,context),undefined,'approved op must pass the gate');
    assert.equal(hooks.get('tool_call')(approvedCall,context).block,true,'one-time consumption');
    assert.equal(hooks.get('tool_call')(bash,context).block,true,'unapproved op stays blocked');

    // The approve prompt never changes the armed state.
    out = await hook({prompt:'nothing to see here',systemPrompt:'B'},context);
    assert.ok(out.systemPrompt.includes('armed'),'armed notice must persist after approvals');
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});

test('composer approve failures: missing session, bad record, non-json path', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const hooks = new Map();
  create({registerCommand:() => {},on:(name,handler) => hooks.set(name,handler)});
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-approve-err-'));
  try {
    const hook = hooks.get('before_agent_start');
    // Missing session identity: refuse without throwing.
    const noId = {cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      sessionManager:{getSessionId:() => null},ui:{notify:() => {},confirm:async () => true}};
    const out1 = await hook({prompt:'/goalanchor-approve a.json',systemPrompt:'B'},noId);
    assert.ok(out1.systemPrompt.includes('session identity unavailable'));
    // Non-json suffix: jail message.
    const ctx = {cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      sessionManager:{getSessionId:() => 'ERR-S1'},ui:{notify:() => {},confirm:async () => true}};
    hook({prompt:'/goalanchor-guard on',systemPrompt:'B'},ctx);
    const out2 = await hook({prompt:'/goalanchor-approve record.txt',systemPrompt:'B'},ctx);
    assert.ok(out2.systemPrompt.includes('nothing approved'));
    assert.ok(out2.systemPrompt.includes('preflight failed'));
    // Missing record file: same fail-closed notice shape.
    const out3 = await hook({prompt:'/goalanchor-approve missing.json',systemPrompt:'B'},ctx);
    assert.ok(out3.systemPrompt.includes('nothing approved'));
    // Bare command with no path.
    const out4 = await hook({prompt:'/goalanchor-approve',systemPrompt:'B'},ctx);
    assert.ok(out4.systemPrompt.includes('Supply a workspace record path'));
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});

test('composer grant survives exactly one agent_end, then revokes', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const fixture = JSON.parse(await fs.readFile(path.join(plugin,'examples','approval.json'),'utf8'));
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-survive-test-'));
  try {
    await fs.writeFile(path.join(directory,'approval.json'),JSON.stringify(fixture));
    const hooks = new Map();
    create({registerCommand:() => {},on:(name,handler) => hooks.set(name,handler)});
    const context={cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      sessionManager:{getSessionId:() => 'SURV-S1'},ui:{notify:() => {},confirm:async () => true}};
    const hook = hooks.get('before_agent_start');
    const op = fixture.operations[0];
    const call = {toolName:op.tool,input:op.input,toolCallId:'s1'};

    // Turn 1: arm (epoch 1).
    hook({prompt:'/goalanchor-guard on',systemPrompt:'B'},context);
    // Turn 2 (epoch 2): approve grants, bound to this turn's epoch.
    const out = await hook({prompt:'/goalanchor-approve approval.json',systemPrompt:'B'},context);
    assert.ok(out.systemPrompt.includes('approved once'));
    // End of the approve turn: the grant survives this agent_end exactly once.
    hooks.get('agent_end')({},context);
    assert.equal(hooks.get('tool_call')(call,context),undefined,'grant must survive the approve turn end');
    // End of the next run: leftovers revoke.
    hooks.get('agent_end')({},context);
    assert.equal(hooks.get('tool_call')(call,context).block,true,'second agent_end must revoke');
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});

test('palette grant is still revoked at the first agent_end (stale protection intact)', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const fixture = JSON.parse(await fs.readFile(path.join(plugin,'examples','approval.json'),'utf8'));
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-palette-eot-'));
  try {
    await fs.writeFile(path.join(directory,'approval.json'),JSON.stringify(fixture));
    const commands = new Map(); const hooks = new Map();
    create({registerCommand:(name,definition) => commands.set(name,definition),on:(name,handler) => hooks.set(name,handler)});
    const context={cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      sessionManager:{getSessionId:() => 'PAL-S1'},ui:{notify:() => {},confirm:async () => true}};
    const op = fixture.operations[0];
    const call = {toolName:op.tool,input:op.input,toolCallId:'p1'};
    await commands.get('goalanchor-guard').handler('on',context);
    // A run started earlier: the host bumps the epoch as it would per turn.
    hooks.get('before_agent_start')({prompt:'run the approved op',systemPrompt:'B'},context);
    await commands.get('goalanchor-approve').handler('approval.json',context);
    // The grant was made between turns (no new epoch since), so the first
    // agent_end revokes it - stale protection and palette semantics unchanged.
    hooks.get('agent_end')({},context);
    assert.equal(hooks.get('tool_call')(call,context).block,true,
      'palette grant must be revoked at the first agent_end (stale protection)');
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});

test('same-turn palette approve after a composer grant does not inherit the epoch pairing', {skip:!plugin}, async () => {
  const {pathToFileURL} = require('node:url');
  const create = (await import(pathToFileURL(path.join(plugin,'extension.mjs')).href)).default;
  const fixture = JSON.parse(await fs.readFile(path.join(plugin,'examples','approval.json'),'utf8'));
  const directory = await fs.mkdtemp(path.join(os.tmpdir(),'goalanchor-mixed-grant-'));
  try {
    await fs.writeFile(path.join(directory,'approval.json'),JSON.stringify(fixture));
    const commands = new Map(); const hooks = new Map();
    create({registerCommand:(name,definition) => commands.set(name,definition),on:(name,handler) => hooks.set(name,handler)});
    const context={cwd:directory,model:{provider:'p',id:'m'},signal:new AbortController().signal,
      sessionManager:{getSessionId:() => 'MIX-S1'},ui:{notify:() => {},confirm:async () => true}};
    const hook = hooks.get('before_agent_start');
    const op = fixture.operations[0];
    const paletteCall = {toolName:op.tool,input:op.input,toolCallId:'m1'};
    hook({prompt:'/goalanchor-guard on',systemPrompt:'B'},context);
    // Composer grant (epoch pairing set)...
    const out = await hook({prompt:'/goalanchor-approve approval.json',systemPrompt:'B'},context);
    assert.ok(out.systemPrompt.includes('approved once'));
    // ...then a palette approve in the same turn replaces the grant.
    await commands.get('goalanchor-approve').handler('approval.json',context);
    // The palette grant must NOT inherit the pairing: first agent_end revokes.
    hooks.get('agent_end')({},context);
    assert.equal(hooks.get('tool_call')(paletteCall,context).block,true,
      'palette grant after a composer grant must be revoked at the first agent_end');
  } finally { await fs.rm(directory,{recursive:true,force:true}); }
});
