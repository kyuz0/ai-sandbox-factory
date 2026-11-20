import { randomUUID } from 'node:crypto';
import { getBoundaryBroker } from './packages/pi-auto-review/src/broker/index.ts';
import { managedChild } from './packages/pi-auto-review/src/managed-child.ts';
import { trackReviewBranch } from './packages/pi-auto-review/src/review/retained-context.ts';
import { runManagedApprovalDialog } from './packages/pi-reviewed-escalation/dialog-admission.ts';

export function reviewedActions() {
  const mode = process.env.SANDBOX_APPROVAL_MODE;
  if (process.env.PI_SANDBOX_CONTAINER !== '1' || !['automatic', 'assisted', 'manual'].includes(mode)) return;
  let queue = Promise.resolve();

  return async function approve({ ctx, signal, toolCallId, requestFacts, title, details }) {
    const sessionId = ctx.sessionManager.getSessionId();
    if (managedChild(sessionId)) return { reason: 'Ask the parent session to request this action.' };
    const broker = getBoundaryBroker();
    if (!broker) return { reason: 'Approval is unavailable. The action was blocked.' };

    const currentBranch = trackReviewBranch(ctx.sessionManager, ['ask_user_question']);
    const isCurrent = () => !signal?.aborted && currentBranch();
    const request = { id: randomUUID(), source: 'sandbox-runtime', cwd: ctx.cwd, toolCallId, ...requestFacts };
    const decision = await broker.review(request, { sessionId, scopeKey: sessionId, signal, isCurrent, issueGrant: true });
    if (!isCurrent()) return { reason: 'The request was cancelled or changed.' };
    if (decision.kind === 'deny') {
      const policy = decision.denialSource === 'hard-deny' || decision.review.preflightBlocked;
      const label = policy ? 'Blocked by sandbox policy' : decision.denialSource === 'reviewer'
        ? 'Reviewer denied access' : 'Approval unavailable';
      return { reason: label + '. ' + decision.review.rationale };
    }
    if (decision.kind === 'defer') {
      const confirmation = decision.confirmation;
      if (!ctx.hasUI || (mode !== 'manual' && !confirmation)) return { reason: 'This action requires your approval.' };
      const promptSignal = signal && confirmation?.signal
        ? AbortSignal.any([signal, confirmation.signal]) : signal ?? confirmation?.signal;
      const canConfirm = () => isCurrent() && (confirmation?.isCurrent() ?? true) && !promptSignal?.aborted;
      const present = async () => {
        if (!canConfirm()) return false;
        const sections = [title, ...details];
        if (confirmation) sections.push('Reviewer\n' + confirmation.reason);
        const choice = await ctx.ui.select(sections.join('\n\n'), ['Allow once', 'Deny'], { signal: promptSignal });
        if (choice !== 'Allow once' || !canConfirm()) return false;
        if (!confirmation?.critical) return true;
        return await ctx.ui.input('The reviewer rated this request critical. Type APPROVE to allow it once.',
          undefined, { signal: promptSignal }) === 'APPROVE' && canConfirm();
      };
      const answer = process.env.SANDBOX_SUBAGENTS === '1'
        ? runManagedApprovalDialog(sessionId, present) : queue.then(present);
      queue = answer.then(() => {}, () => {});
      if (!await answer || !canConfirm()) return { reason: 'The request was declined or cancelled.' };
    } else if (!decision.grant || !broker.consumeGrant(request, sessionId, decision.grant.token)) {
      return { reason: 'The one-time approval expired. The action was blocked.' };
    }
    return isCurrent() ? {} : { reason: 'The request was cancelled or changed.' };
  };
}
