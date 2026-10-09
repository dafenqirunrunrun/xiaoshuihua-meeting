/**
 * Run-ownership rules for the Captain panel (ADR-002 Phase C.5 §5).
 *
 * Why this is a separate, dependency-free module
 * ----------------------------------------------
 * These rules are the whole fix for the stale-run bug, and they are pure: given
 * "which report is the client waiting on" and "did a terminal event arrive",
 * they say whether a piece of stream traffic belongs to the current run. Keeping
 * them inside the hook would make them testable only through React, and this
 * project has no component-test runner — so the rule that decides whether the
 * user sees a stale judgement or the right one would ship with no executable
 * check at all. Here it is a function of plain values, tested directly.
 *
 * The hook owns one `CaptainRunOwnership` in a ref and calls these functions;
 * the hook itself holds no ownership state of its own.
 *
 * The domain, in one paragraph
 * ----------------------------
 * A meeting accumulates captain reports over time (append-only, throttled), and
 * each carries a unique `report_id`. The client asks for a new one, then waits.
 * Three things arrive asynchronously and can interleave:
 *
 *   - a `GET` response for a report it asked for (authoritative)
 *   - a terminal SSE event (`captain.completed` / `captain.failed`)
 *   - a `captain.quick_assessment` SSE event (a provisional first pass)
 *
 * The failure this module prevents: a quick event for run B arriving while the
 * client is really still showing run A — or worse, arriving for a run A that has
 * already reached a terminal state. Rendering it would pin a provisional
 * judgement to the wrong report. Every rule below exists to make one of those
 * orderings impossible rather than unlikely.
 */

/** Report statuses that mean "this run is over; the durable row is the truth". */
export const TERMINAL_STATUSES = ['COMPLETED', 'FAILED'] as const

export function isTerminalStatus(status: string | null): boolean {
  return status === 'COMPLETED' || status === 'FAILED'
}

export interface CaptainRunOwnership {
  /** The report the client is currently waiting on / showing, or null. */
  reportId: string | null
  /** That report's last known status, or null before anything is known. */
  status: string | null
  /**
   * Report ids known to have reached a terminal state.
   *
   * Separate from `status` because it must survive the window in which a
   * terminal EVENT has arrived but the authoritative GET has not returned yet.
   * A report that emitted a terminal event must never accept a late quick,
   * regardless of what `status` currently says.
   */
  settled: Set<string>
}

export function createCaptainRunOwnership(): CaptainRunOwnership {
  return { reportId: null, status: null, settled: new Set() }
}

/** Return to the initial state. Called on meeting change / disable. */
export function resetRun(o: CaptainRunOwnership): void {
  o.reportId = null
  o.status = null
  o.settled.clear()
}

/**
 * Claim the run the client is about to wait on, BEFORE the GET returns.
 *
 * Both `trigger` (which learns the id from the 202) and a manual refresh with an
 * explicit id call this. Without it the client would spend the whole GET round
 * trip still identifying as the previous run, and a `quick` for the run it just
 * asked for would be discarded as unrelated — silently degrading the two-stage
 * design back to a single stage.
 *
 * `null` means "unknown id, read latest" and deliberately does NOT clear the
 * current identity: the latest read will settle it when it lands, and clearing
 * here would make the client forget which run it is showing with nothing to
 * replace it.
 */
export function claimRun(o: CaptainRunOwnership, reportId: string | null): void {
  if (!reportId) return
  o.reportId = reportId
  o.status = null
}

/**
 * Record what an authoritative GET actually returned.
 *
 * Returns whether the caller must drop any provisional quick result: once a run
 * is durably COMPLETED or FAILED, its quick snapshot is superseded (on success
 * the stored `quick` is authoritative; on failure it would imply the analysis
 * secretly worked).
 */
export function applyLoadedRun(
  o: CaptainRunOwnership,
  reportId: string,
  status: string,
): { clearQuick: boolean } {
  o.reportId = reportId
  o.status = status
  if (!isTerminalStatus(status)) return { clearQuick: false }
  o.settled.add(reportId)
  return { clearQuick: true }
}

export type TerminalDecision =
  /** The event belongs to a run the client is no longer showing. Drop it. */
  | { action: 'ignore' }
  /** The event belongs to the current run (or an unknown id): re-read it. */
  | { action: 'load'; reportId: string | null }

/**
 * Decide what a terminal SSE event means.
 *
 * The `ignore` case is the point: an earlier run finishing late must neither
 * overwrite the report the user is now looking at nor invalidate the GET that
 * run is currently in flight. Both would be the client inventing a history the
 * server never had.
 */
export function decideTerminal(
  o: CaptainRunOwnership,
  reportId: string | null,
): TerminalDecision {
  if (reportId && o.reportId && reportId !== o.reportId) {
    return { action: 'ignore' }
  }
  if (reportId) {
    // Settle immediately, before the GET: a quick for this report arriving
    // during the round trip must already be refused.
    o.settled.add(reportId)
    o.reportId = reportId
  }
  // Status is reset rather than guessed — the GET is about to say what it is.
  // `settled` is what carries the "do not accept quick" fact across this window.
  o.status = null
  return { action: 'load', reportId }
}

/**
 * Decide whether a `captain.quick_assessment` belongs on screen.
 *
 * Two independent refusals, both required:
 *   1. identity — must be the run being shown, not a previous or future one;
 *   2. liveness — must not be a run already known to be terminal.
 */
export function decideQuick(o: CaptainRunOwnership, reportId: string): boolean {
  if (o.reportId !== reportId) return false
  if (o.settled.has(reportId)) return false
  return !isTerminalStatus(o.status)
}
