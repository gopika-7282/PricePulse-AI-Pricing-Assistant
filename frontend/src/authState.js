export function createEmptyLoginCredentials() {
  return { email: '', password: '' };
}

export function createEmptyForgotPasswordState() {
  return { email: '' };
}

export function createEmptyResetPasswordState() {
  return { newPassword: '', confirmPassword: '' };
}

export const INVALID_RESET_LINK_MESSAGE = 'This password reset link is invalid or has expired. Please request a new reset link.';

export function readResetToken(search = globalThis.location?.search || '') {
  const params = new URLSearchParams(search);
  return params.get('token') || params.get('reset_token') || '';
}

export function forgotPasswordNotice(result) {
  if (result?.email_delivery_configured === false) {
    return 'Password reset email delivery is not configured yet. Please configure SMTP to receive reset instructions.';
  }
  return 'If an account exists for this email, reset instructions have been sent.';
}

export function createSubmissionLock() {
  let locked = false;
  return {
    acquire() {
      if (locked) return false;
      locked = true;
      return true;
    },
    release() { locked = false; },
  };
}

export async function submitResetPasswordIfConfirmed(values, submit) {
  if (!values.newPassword?.trim()) {
    throw new Error('Enter a new password.');
  }
  if (!values.confirmPassword?.trim()) {
    throw new Error('Confirm your new password.');
  }
  if (values.newPassword !== values.confirmPassword) {
    throw new Error('Passwords do not match.');
  }
  return submit(values.newPassword);
}

export async function submitPasswordReset(token, values, submit) {
  if (!token) throw new Error(INVALID_RESET_LINK_MESSAGE);
  return submitResetPasswordIfConfirmed(values, password => submit(token, password));
}

export function passwordResetErrorMessage(error) {
  if (/invalid|expired/i.test(error?.message || '')) return INVALID_RESET_LINK_MESSAGE;
  return error?.message || 'Unable to reset your password. Please try again.';
}
