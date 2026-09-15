#!/usr/bin/env node
// Offline wire-format probe against the exact SDK installed in a DSH runtime.
// Usage: node scripts/dsh_multimodal_probe.mjs /absolute/path/to/dsh-runtime
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const runtime = process.argv[2] ?? process.env.DSH_RUNTIME;
if (!runtime) throw new Error('Set DSH_RUNTIME or pass the DSH runtime directory as the first argument');
const sdk = resolve(runtime, 'node_modules/@earendil-works/pi-ai/dist/api');
const imageData = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZAAAAABJRU5ErkJggg==';
const imageUrl = `data:image/png;base64,${imageData}`;
const context = {
  systemPrompt: 'Offline serialization probe.',
  messages: [{ role: 'user', timestamp: 0, content: [
    { type: 'text', text: 'Describe this image.' },
    { type: 'image', mimeType: 'image/png', data: imageData },
  ] }],
};
// Fail closed if any path ignores the injected fetch implementation.
const originalFetch = globalThis.fetch;
globalThis.fetch = async () => { throw new Error('Real network access forbidden by offline probe'); };
try {
  for (const api of ['openai-completions', 'openai-responses']) {
    const { stream } = await import(pathToFileURL(resolve(sdk, `${api}.js`)).href);
    for (const effort of ['high', 'xhigh', 'max']) {
      const wireEffort = effort === 'max' ? 'ultra' : effort;
      const model = {
        id: 'offline-probe-model', name: 'Offline Probe', api, provider: 'codex-bridge',
        baseUrl: 'http://offline.invalid/v1', reasoning: true,
        thinkingLevelMap: { high: 'high', xhigh: 'xhigh', max: 'ultra' }, input: ['text', 'image'],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 262144, maxTokens: 32768,
        compat: api === 'openai-completions'
          ? { thinkingFormat: 'openai', supportsReasoningEffort: true }
          : {},
      };
      let captured;
      const events = stream(model, context, {
        apiKey: 'offline-placeholder-not-a-credential', reasoningEffort: effort, maxRetries: 0,
        fetch: async (url, init) => {
          assert.equal(captured, undefined, 'Must issue only one mocked request');
          captured = { url: String(url), body: JSON.parse(init.body) };
          // Deliberately terminate after serialization; no response parsing is under test.
          return new Response(JSON.stringify({ error: { message: 'Offline capture complete' } }), {
            status: 400, headers: { 'content-type': 'application/json' },
          });
        },
      });
      for await (const _event of events) { /* drain the intentional error event */ }
      assert.ok(captured, `${api} must call the mocked fetch`);
      assert.equal(captured.body.stream, true);
      let image;
      if (api === 'openai-completions') {
        assert.equal(captured.url, 'http://offline.invalid/v1/chat/completions');
        assert.equal(captured.body.reasoning_effort, wireEffort);
        image = captured.body.messages.find(m => m.role === 'user').content.find(c => c.type === 'image_url');
        assert.equal(image.image_url.url, imageUrl);
      } else {
        assert.equal(captured.url, 'http://offline.invalid/v1/responses');
        assert.deepEqual(captured.body.reasoning, { effort: wireEffort, summary: 'auto' });
        assert.deepEqual(captured.body.include, ['reasoning.encrypted_content']);
        image = captured.body.input.find(m => m.role === 'user').content.find(c => c.type === 'input_image');
        assert.equal(image.image_url, imageUrl);
        assert.equal(image.detail, 'auto');
      }
      console.log(JSON.stringify({ api, effort, url: captured.url, reasoning: captured.body.reasoning ?? captured.body.reasoning_effort, imageType: image.type, inlineImagePreserved: true, status: 'PASS' }));
    }
  }
} finally {
  globalThis.fetch = originalFetch;
}
