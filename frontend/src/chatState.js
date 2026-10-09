export function appendChatMessage(messages, message) {
  return [...messages, message];
}

export function restoreChatMessages(conversation) {
  return Array.isArray(conversation?.messages) ? conversation.messages : [];
}

const CHAT_SESSION_PREFIX = 'pp-chat:';
const ACTIVE_CONVERSATION_KEY = `${CHAT_SESSION_PREFIX}active`;

function conversationKey(productId) {
  return `${CHAT_SESSION_PREFIX}product:${productId == null ? 'general' : productId}`;
}

function readConversation(key, storage) {
  try {
    const value = storage?.getItem(key);
    return value ? JSON.parse(value) : null;
  } catch {
    return null;
  }
}

export function getChatConversation(productId, storage = globalThis.sessionStorage) {
  return readConversation(conversationKey(productId), storage);
}

export function getActiveChatConversation(storage = globalThis.sessionStorage) {
  return readConversation(ACTIVE_CONVERSATION_KEY, storage);
}

export function rememberChatConversation(conversation, storage = globalThis.sessionStorage) {
  if (!conversation?.id || !storage) return;
  const record = JSON.stringify({ id: conversation.id, product_id: conversation.product_id ?? null });
  storage.setItem(conversationKey(conversation.product_id), record);
  storage.setItem(ACTIVE_CONVERSATION_KEY, record);
}

export function clearChatSession(storage = globalThis.sessionStorage) {
  if (!storage) return;
  const keys = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(CHAT_SESSION_PREFIX)) keys.push(key);
  }
  keys.forEach(key => storage.removeItem(key));
}
