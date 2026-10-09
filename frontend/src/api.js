const API = import.meta.env?.VITE_API_URL || 'http://localhost:8000';
const SERVER_UNAVAILABLE = 'Unable to connect to the PricePulse server. Please try again in a moment.';
const SERVER_ERROR = 'Something went wrong on the PricePulse server. Please try again later.';

function logApiError(kind, path, error) {
  console.error(`[PricePulse API] ${kind}`, { path, error });
}

function connectionError(error, path, isAnalysis = false) {
  logApiError('connection failure', path, error);
  if (error?.name === 'TimeoutError' || error?.name === 'AbortError') {
    return new Error(isAnalysis
      ? 'The pricing analysis took too long. Please try again.'
      : 'The service is taking too long to respond. Please try again.');
  }
  return new Error(SERVER_UNAVAILABLE);
}

async function readResponse(response, path, onUnauthorized, fallback) {
  let data = null;
  if (response.status !== 204) {
    try {
      data = await response.json();
    } catch (error) {
      logApiError('invalid response body', path, error);
      if (response.ok) throw new Error('The server returned an unexpected response. Please try again.');
    }
  }
  if (response.ok) return data;
  if (response.status === 401) onUnauthorized?.();
  if (response.status === 500 || response.status >= 500 && response.status !== 503) throw new Error(SERVER_ERROR);
  const messages = {
    401: 'Your session has expired. Please sign in again.',
    403: 'You do not have permission to do that.',
    404: 'That product could not be found.',
    409: 'We could not complete that product request. Please review the details and try again.',
    422: 'Please check the product details and try again.',
    503: path.startsWith('/api/chat') ? 'The assistant is temporarily unavailable. Please try again shortly.' : 'Price analysis could not be completed right now. Please try again.',
  };
  const detail = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
  const publicDetail = response.status >= 500 && !(response.status === 503 && path.startsWith('/api/chat')) || response.status === 401 ? undefined : detail;
  throw new Error(publicDetail || messages[response.status] || fallback || 'We could not complete that request. Please try again.');
}

export async function call(path, { token, onUnauthorized, ...options } = {}) {
  let response;
  try {
    const timeout = path.includes('/analyze') ? 240000 : path === '/api/chat' ? 360000 : 30000;
    response = await fetch(`${API}${path}`, { ...options, signal: options.signal || AbortSignal.timeout(timeout), headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options?.headers } });
  } catch (error) {
    throw connectionError(error, path, path.includes('/analyze'));
  }
  return readResponse(response, path, onUnauthorized, 'We could not complete that request. Please try again.');
}

export function clearSession(storage = globalThis.localStorage) {
  storage?.removeItem('pp-token');
}

export function loadAuthenticatedUser(token, onUnauthorized) {
  return call('/auth/me', { token, onUnauthorized });
}

export async function loadRetailerProducts(token, onUnauthorized) {
  const products = await call('/api/products', { token, onUnauthorized });
  if (!Array.isArray(products)) throw new Error('We couldn’t load your products. Please try again.');
  return products;
}

export function saveRetailerProduct(token, product, existingProduct, onUnauthorized) {
  return call(existingProduct ? `/api/products/${existingProduct.id}` : '/api/products', {
    token,
    onUnauthorized,
    method: existingProduct ? 'PUT' : 'POST',
    body: JSON.stringify(product),
  });
}

export async function streamPricingWorkflow(retailerProductId, token, onProgress, onUnauthorized) {
  let response;
  try {
    response = await fetch(`${API}/api/workflow/${retailerProductId}/analyze/stream`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' },
      // A full workflow can include multiple bounded model calls and marketplace checks.
      signal: AbortSignal.timeout(1200000),
    });
  } catch (error) {
    throw connectionError(error, `/api/workflow/${retailerProductId}/analyze/stream`, true);
  }
  if (!response.ok) {
    await readResponse(response, `/api/workflow/${retailerProductId}/analyze/stream`, onUnauthorized, 'We couldn’t complete the pricing analysis. Please try again.');
  }
  if (!response.body) {
    logApiError('missing event stream body', `/api/workflow/${retailerProductId}/analyze/stream`, new Error('Response body unavailable'));
    throw new Error('We couldn’t receive the pricing analysis update. Please try again.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finalResult = null;
  const processFrame = frame => {
    const dataLine = frame.split(/\r?\n/).find(line => line.startsWith('data:'));
    if (!dataLine) return;
    let payload;
    try { payload = JSON.parse(dataLine.slice(5).trim()); }
    catch (error) {
      logApiError('invalid workflow event', `/api/workflow/${retailerProductId}/analyze/stream`, error);
      throw new Error('Price analysis could not be completed right now. Please try again.');
    }
    if (payload.type === 'progress') onProgress?.(payload);
    else if (payload.type === 'result') finalResult = payload.result;
    else if (payload.type === 'error') {
      logApiError('workflow reported an error', `/api/workflow/${retailerProductId}/analyze/stream`, payload.message);
      throw new Error('We couldn’t complete the pricing analysis. Please try again.');
    }
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const frames = buffer.split(/\r?\n\r?\n/);
      buffer = frames.pop() || '';
      frames.forEach(processFrame);
      if (done) break;
    }
  } catch (error) {
    if (error.message?.startsWith('We couldn’t')) throw error;
    throw connectionError(error, `/api/workflow/${retailerProductId}/analyze/stream`, true);
  }
  if (buffer.trim()) processFrame(buffer);
  if (!finalResult) throw new Error('We couldn’t complete the pricing analysis. Please try again.');
  return finalResult;
}

export function acceptRecommendation(token, recommendationId, onUnauthorized) {
  return call(`/api/recommendations/${recommendationId}/accept`, { token, onUnauthorized, method: 'POST' });
}

export function loadLatestChat(token, productId, onUnauthorized) {
  const query = productId == null ? '' : `?retailer_product_id=${encodeURIComponent(productId)}`;
  return call(`/api/chat/conversations/latest${query}`, { token, onUnauthorized });
}

export function createChatConversation(token, productId, onUnauthorized) {
  return call('/api/chat/conversations', { token, onUnauthorized, method: 'POST',
    body: JSON.stringify({ retailer_product_id: productId ?? null }) });
}

export function loadChatConversation(token, conversationId, onUnauthorized) {
  return call(`/api/chat/conversations/${encodeURIComponent(conversationId)}`, { token, onUnauthorized });
}

export function sendChatMessage(token, conversationId, productId, question, onUnauthorized) {
  return call('/api/chat', { token, onUnauthorized, method: 'POST',
    body: JSON.stringify({ conversation_id: conversationId, retailer_product_id: productId ?? null, question }) });
}
