import test from 'node:test';
import assert from 'node:assert/strict';
import { call, clearSession, createChatConversation, loadChatConversation, loadAuthenticatedUser, loadRetailerProducts, saveRetailerProduct, streamPricingWorkflow, acceptRecommendation } from '../src/api.js';
import { toggleProductList } from '../src/productListState.js';
import { appendChatMessage, clearChatSession, getActiveChatConversation, getChatConversation, rememberChatConversation, restoreChatMessages } from '../src/chatState.js';
import { createEmptyForgotPasswordState, createEmptyLoginCredentials, createEmptyResetPasswordState, createSubmissionLock, forgotPasswordNotice, INVALID_RESET_LINK_MESSAGE, passwordResetErrorMessage, readResetToken, submitPasswordReset, submitResetPasswordIfConfirmed } from '../src/authState.js';

test('each sign-in mount starts with empty email and password credentials', () => {
  const firstMount = createEmptyLoginCredentials();
  firstMount.email = 'user-a@example.test';
  firstMount.password = 'NeverPersistThis1!';
  const afterLogout = createEmptyLoginCredentials();
  const afterReload = createEmptyLoginCredentials();
  assert.deepEqual(afterLogout, { email: '', password: '' });
  assert.deepEqual(afterReload, { email: '', password: '' });
  assert.notEqual(afterLogout, firstMount);
});

test('forgot-password and reset forms start empty and do not inherit sign-in email', async () => {
  const login = createEmptyLoginCredentials();
  login.email = 'remembered@example.test';
  const forgot = createEmptyForgotPasswordState();
  const reset = createEmptyResetPasswordState();
  assert.deepEqual(forgot, { email: '' });
  assert.deepEqual(reset, { newPassword: '', confirmPassword: '' });
  assert.notEqual(forgot.email, login.email);
  assert.notEqual(createEmptyForgotPasswordState(), forgot);
});

test('forgot-password notice distinguishes unconfigured SMTP from configured delivery without identifying the account', () => {
  assert.match(forgotPasswordNotice({ email_delivery_configured: false, message: 'Internal mail diagnostic' }), /email delivery is not configured/i);
  assert.doesNotMatch(forgotPasswordNotice({ email_delivery_configured: false }), /SMTP_PASSWORD|diagnostic|reset_token/i);
  const sent = forgotPasswordNotice({ email_delivery_configured: true });
  assert.equal(sent, 'If an account exists for this email, reset instructions have been sent.');
  assert.doesNotMatch(sent, /registered|exists: yes/i);
});

test('single-flight submission lock allows exactly one in-flight forgot-password request', async () => {
  const lock = createSubmissionLock();
  let requests = 0;
  const send = async () => {
    if (!lock.acquire()) return;
    requests += 1;
    await Promise.resolve();
    lock.release();
  };
  await Promise.all([send(), send()]);
  assert.equal(requests, 1);
  assert.equal(lock.acquire(), true);
  lock.release();
});

test('forgot-password API failure shows the friendly connection error with one request', async () => {
  const originalFetch = globalThis.fetch;
  const originalConsoleError = console.error;
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new TypeError('Failed to fetch'); };
  console.error = () => {};
  try {
    await assert.rejects(call('/auth/forgot-password', { method: 'POST', body: JSON.stringify({ email: 'retailer@example.test' }) }), /Unable to connect to the PricePulse server/);
    assert.equal(requests, 1);
  } finally { globalThis.fetch = originalFetch; console.error = originalConsoleError; }
});

test('forgot-password loading and back-to-sign-in use one form submit path and reset sign-in credentials', async () => {
  const fs = await import('node:fs/promises');
  const frontend = await fs.readFile(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.match(frontend, /onSubmit=\{submit\}/);
  assert.match(frontend, /disabled=\{busy\s*\|\|/);
  assert.match(frontend, /screen === 'forgot' \? 'Sending…'/);
  assert.match(frontend, /onClick=\{\(\) => showScreen\('login'\)\}/);
  assert.match(frontend, /setCredentials\(createEmptyLoginCredentials\(\)\)/);
  assert.doesNotMatch(frontend, /onClick=\{[^}]*forgot-password/);
});

test('reset confirmation mismatch prevents the reset request', async () => {
  let called = false;
  await assert.rejects(submitResetPasswordIfConfirmed({ newPassword: 'NewPass2!', confirmPassword: 'Different3!' }, async () => { called = true; }), /^Error: Passwords do not match\.$/);
  assert.equal(called, false);
});

test('matching reset confirmation submits only the new password', async () => {
  let submitted;
  const result = await submitResetPasswordIfConfirmed({ newPassword: 'NewPass2!', confirmPassword: 'NewPass2!' }, async password => { submitted = password; return 'ok'; });
  assert.equal(submitted, 'NewPass2!');
  assert.equal(result, 'ok');
});

test('reset page route reads the token query and keeps it out of persistent storage', async () => {
  assert.equal(readResetToken('?token=one-time%2Btoken'), 'one-time+token');
  assert.equal(readResetToken('?reset_token=legacy-token'), 'legacy-token');
  const fs = await import('node:fs/promises');
  const frontend = await fs.readFile(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.match(frontend, /pathname\.replace\(.*\/reset-password/);
  assert.match(frontend, /readResetToken\(window\.location\.search\)/);
  assert.doesNotMatch(frontend, /localStorage\.(?:setItem|getItem)\(['"]reset[_-]?token/i);
});

test('reset password requires both nonempty values and a matching confirmation', async () => {
  let called = false;
  await assert.rejects(submitPasswordReset('token', { newPassword: '', confirmPassword: '' }, () => { called = true; }), /Enter a new password/);
  await assert.rejects(submitPasswordReset('token', { newPassword: 'NewPass2!', confirmPassword: ' ' }, () => { called = true; }), /Confirm your new password/);
  await assert.rejects(submitPasswordReset('token', { newPassword: 'NewPass2!', confirmPassword: 'OtherPass3!' }, () => { called = true; }), /Passwords do not match/);
  assert.equal(called, false);
});

test('valid reset submission posts the query token and new password to the existing endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, method: options.method, body: JSON.parse(options.body) };
    return { ok: true, status: 200, json: async () => ({ message: 'Password reset successful.' }) };
  };
  try {
    const result = await submitPasswordReset('token-from-query', { newPassword: 'NewPass2!', confirmPassword: 'NewPass2!' }, (token, newPassword) =>
      call('/auth/reset-password', { method: 'POST', body: JSON.stringify({ token, new_password: newPassword }) }));
    assert.equal(request.url, 'http://localhost:8000/auth/reset-password');
    assert.equal(request.method, 'POST');
    assert.deepEqual(request.body, { token: 'token-from-query', new_password: 'NewPass2!' });
    assert.equal(result.message, 'Password reset successful.');
  } finally { globalThis.fetch = originalFetch; }
});

test('reset submission lock prevents duplicate reset API calls', async () => {
  const lock = createSubmissionLock();
  let calls = 0;
  const send = async () => {
    if (!lock.acquire()) return;
    try {
      await submitPasswordReset('token', { newPassword: 'NewPass2!', confirmPassword: 'NewPass2!' }, async () => {
        calls += 1;
        await new Promise(resolve => setTimeout(resolve, 5));
      });
    } finally { lock.release(); }
  };
  await Promise.all([send(), send()]);
  assert.equal(calls, 1);
});

test('reset success and invalid or expired token screens have clear recovery actions', async () => {
  assert.equal(passwordResetErrorMessage(new Error('This password-reset link is invalid or has expired. Request a new one.')), INVALID_RESET_LINK_MESSAGE);
  assert.equal(passwordResetErrorMessage(new Error('expired token')), INVALID_RESET_LINK_MESSAGE);
  assert.equal(passwordResetErrorMessage(new Error('Service unavailable')), 'Service unavailable');
  const fs = await import('node:fs/promises');
  const frontend = await fs.readFile(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.match(frontend, /screen === 'reset-success'/);
  assert.match(frontend, /Password reset successfully/);
  assert.match(frontend, /You can now sign in with your new password/);
  assert.match(frontend, /Back to Sign In/);
  assert.match(frontend, /Request a new reset link/);
  assert.match(frontend, /passwordResetErrorMessage\(err\)/);
});

test('login credential values have no application persistence helper', async () => {
  const source = await import('node:fs/promises');
  const frontend = await source.readFile(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.doesNotMatch(frontend, /localStorage\.(?:setItem|getItem)\(['"](?:email|password)|sessionStorage\.(?:setItem|getItem)\(['"](?:email|password)/i);
});

test('sign out clears the persisted token', () => {
  const values = new Map([['pp-token', 'old-token']]);
  clearSession({ removeItem: key => values.delete(key) });
  assert.equal(values.has('pp-token'), false);
});

test('chat message updates append without replacing earlier conversation messages', () => {
  const earlier = [{ role: 'user', content: 'What is my price?' }, { role: 'assistant', content: '₹158.' }];
  const updated = appendChatMessage(earlier, { role: 'user', content: 'Why?' });
  assert.equal(updated.length, 3);
  assert.equal(updated[0], earlier[0]);
  assert.equal(updated[2].content, 'Why?');
});

test('chat messages restore from the server conversation after reload', () => {
  const saved = [{ id: 1, role: 'user', content: 'What is my price?' }, { id: 2, role: 'assistant', content: '₹158.' }];
  assert.deepEqual(restoreChatMessages({ id: 9, messages: saved }), saved);
  assert.deepEqual(restoreChatMessages({ id: 10 }), []);
});

test('chat conversation pointer survives refresh in session storage but clears at logout', () => {
  const values = new Map();
  const storage = {
    get length() { return values.size; },
    key(index) { return [...values.keys()][index] ?? null; },
    getItem(key) { return values.get(key) ?? null; },
    setItem(key, value) { values.set(key, value); },
    removeItem(key) { values.delete(key); },
  };
  const conversation = { id: 19, product_id: 7, messages: [{ role: 'user', content: 'How should I price it?' }] };
  rememberChatConversation(conversation, storage);
  assert.deepEqual(getChatConversation(7, storage), { id: 19, product_id: 7 });
  // A page refresh keeps sessionStorage, so the new React app can reload this conversation.
  assert.equal(getActiveChatConversation(storage).id, 19);
  clearChatSession(storage);
  assert.equal(getChatConversation(7, storage), null);
  assert.equal(getActiveChatConversation(storage), null);
});

test('chat API creates a fresh conversation and reloads a session-scoped conversation by id', async () => {
  const originalFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, options) => {
    seen.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : null });
    return { ok: true, status: 200, json: async () => ({ id: 42, product_id: 7, messages: [] }) };
  };
  try {
    await createChatConversation('session-token', 7);
    await loadChatConversation('session-token', 42);
    assert.deepEqual(seen, [
      { url: 'http://localhost:8000/api/chat/conversations', method: 'POST', body: { retailer_product_id: 7 } },
      { url: 'http://localhost:8000/api/chat/conversations/42', method: undefined, body: null },
    ]);
  } finally { globalThis.fetch = originalFetch; }
});

test('logout and successful login clear session-only chat conversation pointers', async () => {
  const fs = await import('node:fs/promises');
  const frontend = await fs.readFile(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.match(frontend, /const startSession = nextToken => \{\s*clearChatSession\(\)/);
  assert.match(frontend, /clearSession\(\); clearChatSession\(\)/);
  assert.match(frontend, /getActiveChatConversation\(\)/);
  assert.match(frontend, /rememberChatConversation\(conversation\)/);
  assert.doesNotMatch(frontend, /loadLatestChat/);
});

test('expired credentials request session clearing and show a sign-in message', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 401, json: async () => ({ detail: 'Invalid token' }) });
  let cleared = false;
  try {
    await assert.rejects(call('/api/products', { token: 'expired', onUnauthorized: () => { cleared = true; } }), /session has expired/i);
    assert.equal(cleared, true);
  } finally { globalThis.fetch = originalFetch; }
});

test('ordinary service errors do not clear the session', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ detail: 'internal diagnostic' }) });
  let cleared = false;
  try {
    await assert.rejects(call('/api/products', { token: 'valid', onUnauthorized: () => { cleared = true; } }), /price analysis could not be completed right now/i);
    assert.equal(cleared, false);
  } finally { globalThis.fetch = originalFetch; }
});

test('chat service errors show the safe chat-specific message', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({ detail: "I couldn't save the conversation right now. Please try again." }) });
  try {
    await assert.rejects(call('/api/chat', { token: 'valid' }), /couldn't save the conversation/i);
  } finally { globalThis.fetch = originalFetch; }
});

test('backend connection failures use a friendly message and hide browser fetch text', async () => {
  const originalFetch = globalThis.fetch;
  const originalConsoleError = console.error;
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  console.error = () => {};
  try {
    await assert.rejects(call('/auth/me'), error => {
      assert.equal(error.message, 'Unable to connect to the PricePulse server. Please try again in a moment.');
      assert.doesNotMatch(error.message, /Failed to fetch|localhost|stack/i);
      return true;
    });
    await assert.rejects(streamPricingWorkflow(1, 'token'), /Unable to connect to the PricePulse server/);
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalConsoleError;
  }
});

test('HTTP 500 details are hidden behind a generic server error', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: false, status: 500, json: async () => ({ detail: 'password, database URL, traceback' }) });
  try {
    await assert.rejects(call('/api/products'), error => {
      assert.equal(error.message, 'Something went wrong on the PricePulse server. Please try again later.');
      assert.doesNotMatch(error.message, /database URL|traceback|password/i);
      return true;
    });
  } finally { globalThis.fetch = originalFetch; }
});

test('unexpected successful non-JSON response stays user-friendly', async () => {
  const originalFetch = globalThis.fetch;
  const originalConsoleError = console.error;
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } });
  console.error = () => {};
  try {
    await assert.rejects(call('/api/products'), error => {
      assert.equal(error.message, 'The server returned an unexpected response. Please try again.');
      assert.doesNotMatch(error.message, /Unexpected token|SyntaxError/i);
      return true;
    });
  } finally {
    globalThis.fetch = originalFetch;
    console.error = originalConsoleError;
  }
});

test('authenticated profile comes from /auth/me for the active token', async () => {
  const originalFetch = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, options) => {
    seen.push({ url, authorization: options.headers.Authorization });
    const name = options.headers.Authorization.endsWith('user-a') ? 'Gopika' : 'Retailer B';
    return { ok: true, status: 200, json: async () => ({ id: 1, retailer_name: name }) };
  };
  try {
    assert.equal((await loadAuthenticatedUser('user-a')).retailer_name, 'Gopika');
    assert.equal((await loadAuthenticatedUser('user-b')).retailer_name, 'Retailer B');
    assert.ok(seen.every(request => request.url.endsWith('/auth/me')));
    assert.deepEqual(seen.map(request => request.authorization), ['Bearer user-a', 'Bearer user-b']);
  } finally { globalThis.fetch = originalFetch; }
});

test('View My Products loader requests the authenticated retailer endpoint and parses its array', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, authorization: options.headers.Authorization };
    return { ok: true, status: 200, json: async () => [{ id: 12, product_name: 'Hibiscus Hair Oil', cost_price: 120, stock_quantity: 15 }] };
  };
  try {
    const products = await loadRetailerProducts('retailer-token');
    assert.equal(request.url, 'http://localhost:8000/api/products');
    assert.equal(request.authorization, 'Bearer retailer-token');
    assert.equal(products[0].product_name, 'Hibiscus Hair Oil');
  } finally { globalThis.fetch = originalFetch; }
});

test('View My Products opens with fresh data, closes without fetching, then refreshes when reopened', async () => {
  const originalFetch = globalThis.fetch;
  let count = 0;
  let visible = false;
  let products = [];
  globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => [{ id: ++count, product_name: `Server product ${count}` }] });
  const click = () => toggleProductList(visible, {
    open: async () => { products = await loadRetailerProducts('retailer-token'); visible = true; return products; },
    close: () => { visible = false; return products; },
  });
  try {
    const first = await click();
    assert.equal(visible, true);
    assert.equal(first[0].id, 1);
    await click();
    assert.equal(visible, false);
    assert.equal(count, 1);
    const third = await click();
    assert.equal(visible, true);
    assert.equal(count, 2);
    assert.equal(third[0].id, 2);
  } finally { globalThis.fetch = originalFetch; }
});

test('Add Product submits the form values once to the authenticated product endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, method: options.method, authorization: options.headers.Authorization, body: JSON.parse(options.body) };
    return { ok: true, status: 201, json: async () => ({ id: 18, product_name: request.body.product_name }) };
  };
  const product = { product_name: 'Goat Milk Soap', cost_price: 90, stock_quantity: 20, minimum_profit_margin: 18 };
  try {
    const created = await saveRetailerProduct('retailer-token', product, null);
    assert.equal(request.url, 'http://localhost:8000/api/products');
    assert.equal(request.method, 'POST');
    assert.equal(request.authorization, 'Bearer retailer-token');
    assert.deepEqual(request.body, product);
    assert.equal(created.id, 18);
  } finally { globalThis.fetch = originalFetch; }
});

test('workflow UI can render backend-reported LangGraph events and final result', async () => {
  const originalFetch = globalThis.fetch;
  const events = [
    { type: 'progress', stage: 'product_review', status: 'running', message: 'Checking your product details' },
    { type: 'progress', stage: 'product_review', status: 'completed', message: 'Checking your product details' },
    { type: 'result', result: { recommendation: { id: 9, recommended_price: 179 } } },
  ];
  const wire = events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');
  globalThis.fetch = async (url, options) => ({
    ok: true,
    status: 200,
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(wire)); controller.close(); } }),
    requestUrl: url,
    requestOptions: options,
  });
  const reported = [];
  try {
    const result = await streamPricingWorkflow(13, 'retailer-token', event => reported.push(event));
    assert.equal(result.recommendation.recommended_price, 179);
    assert.deepEqual(reported.map(item => item.status), ['running', 'completed']);
  } finally { globalThis.fetch = originalFetch; }
});

test('workflow error event after HTTP 200 rejects instead of reporting success', async () => {
  const originalFetch = globalThis.fetch;
  const wire = 'data: {"type":"error","message":"workflow failed"}\n\n';
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(wire)); controller.close(); } }),
  });
  try {
    await assert.rejects(streamPricingWorkflow(13, 'retailer-token'), /complete the pricing analysis/);
  } finally { globalThis.fetch = originalFetch; }
});

test('workflow stream ending without a result is incomplete', async () => {
  const originalFetch = globalThis.fetch;
  const wire = 'data: {"type":"progress","stage":"recommendation","status":"running"}\n\n';
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    body: new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(wire)); controller.close(); } }),
  });
  try {
    await assert.rejects(streamPricingWorkflow(13, 'retailer-token'), /complete the pricing analysis/);
  } finally { globalThis.fetch = originalFetch; }
});

test('recommendation card maps persisted price, range, reasoning, and basis after workflow completion', async () => {
  const fs = await import('node:fs/promises');
  const frontend = await fs.readFile(new URL('../src/main.jsx', import.meta.url), 'utf8');
  assert.match(frontend, /const r = result\.repricing\?\.recommendation/);
  assert.match(frontend, /recommended_price_min/);
  assert.match(frontend, /recommended_price_max/);
  assert.match(frontend, /recommendation\.reasoning/);
  assert.match(frontend, /Pricing basis:/);
  assert.match(frontend, /Minimum selling price/);
  assert.match(frontend, /Expected gross profit per pack/);
  assert.match(frontend, /Expected margin on cost/);
  assert.match(frontend, /Headroom above minimum/);
  assert.match(frontend, /marketPriceRangeDisplay \|\| 'Not available'/);
});

test('Accept Price uses the authenticated persistence endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, method: options.method, authorization: options.headers.Authorization };
    return { ok: true, status: 200, json: async () => ({ accepted: true, accepted_price: 179, task_status: 'COMPLETED' }) };
  };
  try {
    const saved = await acceptRecommendation('retailer-token', 9);
    assert.equal(request.url, 'http://localhost:8000/api/recommendations/9/accept');
    assert.equal(request.method, 'POST');
    assert.equal(request.authorization, 'Bearer retailer-token');
    assert.equal(saved.accepted, true);
  } finally { globalThis.fetch = originalFetch; }
});
