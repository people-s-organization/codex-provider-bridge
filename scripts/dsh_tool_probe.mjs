#!/usr/bin/env node
/** Installed DSH definition -> real pi-ai/OpenAI SDK -> existing local bridge.
 * No server/config writes, shell tools, or model-generated code execution.
 * JSON evidence goes to stdout; authentication headers are never captured.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { resolve } from 'node:path';

const runtime = resolve(process.env.DSH_RUNTIME ?? '/home/arjenzhou/src/nova/config/dsh-runtime');
const requireInstalled = createRequire(`${runtime}/package.json`);
// pi-ai exposes import-only conditional exports; createRequire.resolve cannot
// resolve those. Use its inspected installed distribution entry explicitly.
const entry = (name) => name === '@earendil-works/pi-ai/api/openai-completions'
  ? resolve(runtime, 'node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js')
  : requireInstalled.resolve(name);
const installed = (name) => import(pathToFileURL(entry(name)).href);
const baseUrl = new URL(process.env.BRIDGE_BASE_URL ?? 'http://127.0.0.1:8790/v1');
assert(['localhost', '127.0.0.1', '[::1]'].includes(baseUrl.hostname), 'Only loopback bridges are allowed');
assert(!baseUrl.username && !baseUrl.password && !baseUrl.search && !baseUrl.hash, 'No credentials/query/fragment in URL');
const apiKey = process.env.BRIDGE_API_KEY ?? 'dsh-probe-no-secret';
const timeoutMs = Number(process.env.PROBE_TIMEOUT_MS ?? 120000);
assert(Number.isFinite(timeoutMs) && timeoutMs > 0, 'Invalid PROBE_TIMEOUT_MS');
const signal = AbortSignal.timeout(timeoutMs);
const evidence = {
  scope: 'One installed todo plugin activation, not the live DSH registry or an all-tool list',
  runtime, baseUrl: baseUrl.href, requests: [],
};

try {
  const tools = await installed('@deepseek-ai/dsh-tools');
  const todoPlugin = await installed('@deepseek-ai/dsh-tool-todo');
  const { stream } = await installed('@earendil-works/pi-ai/api/openai-completions');
  evidence.installedEntries = Object.fromEntries([
    '@deepseek-ai/dsh-tools', '@deepseek-ai/dsh-tool-todo',
    '@earendil-works/pi-ai/api/openai-completions',
  ].map((name) => [name, entry(name)]));

  // Plugin code itself calls the installed defineTool and schema compiler.
  const definitions = [];
  const projections = [];
  todoPlugin.apply({
    tools: { register: (definition) => definitions.push(definition) },
    sessionProjections: { register: (projection) => projections.push(projection) },
  }, { allowParallelInProgress: true });
  assert.equal(definitions.length, 1);
  const todo = definitions[0];
  assert.equal(todo.name, 'todo_write');
  // Independently exercise the public helpers, without substituting a hand-written
  // todo schema for the registered definition that will actually be transmitted.
  const helperSpec = { value: { type: 'string', required: true } };
  const helperDefinition = tools.defineTool({
    name: 'probe_helper_not_sent', description: 'Schema helper smoke check only',
    parameters: helperSpec,
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    execute: async ({ value }) => value,
  });
  assert.deepEqual(helperDefinition.parameters, tools.parameterSchemaSpecToJsonSchema(helperSpec));
  evidence.definition = { name: todo.name, parameters: todo.parameters, projectionKeys: projections.map((p) => p.key) };

  const modelsResponse = await fetch(`${baseUrl.href.replace(/\/$/, '')}/models`, {
    headers: { Authorization: `Bearer ${apiKey}` }, signal, redirect: 'error',
  });
  assert.equal(modelsResponse.status, 200, `Model discovery HTTP ${modelsResponse.status}`);
  const models = await modelsResponse.json();
  evidence.discoveredModels = models.data.map((model) => model.id);
  const modelId = process.env.BRIDGE_MODEL ?? evidence.discoveredModels[0];
  assert(evidence.discoveredModels.includes(modelId), 'Requested model is not advertised');
  const model = {
    id: modelId, name: modelId, api: 'openai-completions', provider: 'openai',
    baseUrl: baseUrl.href.replace(/\/$/, ''), reasoning: false, input: ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000, maxTokens: 1024,
  };
  evidence.model = modelId;
  const captureFetch = async (input, init) => {
    const request = new Request(input, init);
    const url = new URL(request.url);
    assert.equal(url.origin, baseUrl.origin, 'SDK request must stay on bridge origin');
    const body = JSON.parse(await request.clone().text());
    const capture = { method: request.method, path: url.pathname, body };
    evidence.requests.push(capture); // Body AFTER SDK serialization, not onPayload.
    const response = await fetch(request, { redirect: 'error' });
    capture.status = response.status;
    return response;
  };
  const expectedArgs = { todos: [{ content: 'Verify DSH bridge probe', status: 'completed' }] };
  const context = {
    systemPrompt: 'This is a harmless integration probe. Follow the exact requested tool arguments. Never execute code.',
    messages: [{ role: 'user', content: `Call todo_write exactly once with ${JSON.stringify(expectedArgs)}. After the tool result, reply exactly DSH_PROBE_OK.`, timestamp: Date.now() }],
    tools: [{ name: todo.name, description: todo.description, parameters: todo.parameters }],
  };
  const options = { apiKey, fetch: captureFetch, signal, maxTokens: 1024 };
  const first = await stream(model, context, {
    ...options, toolChoice: { type: 'function', function: { name: todo.name } },
  }).result();
  evidence.firstStopReason = first.stopReason;
  assert(!['error', 'aborted'].includes(first.stopReason), `First stream failed (${first.stopReason}); inspect HTTP statuses`);
  const calls = first.content.filter((block) => block.type === 'toolCall');
  assert.equal(calls.length, 1, 'Expected exactly one tool call');
  const call = calls[0];
  assert.equal(call.name, todo.name);
  assert.deepEqual(call.arguments, expectedArgs, 'Refuse unexpected model arguments');
  const events = [];
  const result = await todo.execute(call.arguments, {
    agent: { session: { append: (type, data) => events.push({ type, data }) } },
  });
  assert.deepEqual(result, { todos: expectedArgs.todos, counts: { pending: 0, inProgress: 0, completed: 1 } });
  assert.deepEqual(events, [{ type: 'todo/write', data: expectedArgs }]);
  const rendered = todo.output.render(call.arguments, result);
  evidence.execution = { call, result, rendered, events, persistence: 'in-memory fake session only' };
  context.messages.push(first, {
    role: 'toolResult', toolCallId: call.id, toolName: call.name,
    content: rendered, isError: false, timestamp: Date.now(),
  });
  const second = await stream(model, context, { ...options, toolChoice: 'none' }).result();
  evidence.secondStopReason = second.stopReason;
  assert(!['error', 'aborted'].includes(second.stopReason), `Replay stream failed (${second.stopReason}); inspect HTTP statuses`);
  assert.equal(second.content.filter((block) => block.type === 'toolCall').length, 0);
  evidence.finalText = second.content.filter((block) => block.type === 'text').map((block) => block.text).join('');
  assert.equal(evidence.finalText.trim(), 'DSH_PROBE_OK');
  const replay = evidence.requests.at(-1).body.messages;
  const wireCall = replay.find((message) => message.role === 'assistant' && message.tool_calls)?.tool_calls[0];
  const wireResult = replay.find((message) => message.role === 'tool');
  assert(wireCall && wireResult, 'Missing assistant tool call or tool result on wire');
  assert.equal(wireCall.id, wireResult.tool_call_id);
  assert.deepEqual(JSON.parse(wireCall.function.arguments), expectedArgs);
  assert.equal(wireResult.content, rendered[0].text);
  for (const request of evidence.requests) {
    assert.equal(request.body.stream, true);
    assert.deepEqual(request.body.tools[0].function.parameters, todo.parameters);
    assert.equal(request.status, 200);
  }
  evidence.ok = true;
} catch (error) {
  evidence.ok = false;
  // Do not print upstream exception objects: SDK errors can include request headers.
  evidence.failure = error instanceof assert.AssertionError ? error.message : `Probe failed (${error?.name ?? 'Error'}); inspect captured statuses and installed APIs`;
  process.exitCode = 1;
}
// Defense in depth for a user-provided key; headers are never recorded at all.
const serialized = JSON.stringify(evidence, null, 2);
console.log(apiKey && apiKey !== 'dsh-probe-no-secret' ? serialized.split(apiKey).join('[REDACTED]') : serialized);
