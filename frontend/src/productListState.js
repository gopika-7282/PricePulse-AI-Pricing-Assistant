export async function toggleProductList(isVisible, { open, close }) {
  if (isVisible) return close();
  return open();
}
