import { reviewedActions } from './reviewed-action.mjs';
import { registerReviewedFileAccess } from './reviewed-file-access.mjs';
import { registerProviderCheck } from './provider-connection-check.mjs';

export default function registerReviewedActions(pi) {
  const approve = reviewedActions();
  if (!approve) return;
  registerReviewedFileAccess(pi, approve);
  registerProviderCheck(pi, approve);
}
