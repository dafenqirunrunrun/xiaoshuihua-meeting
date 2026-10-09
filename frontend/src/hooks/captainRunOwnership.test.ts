/**
 * Executable coverage for the Captain stale-run ownership rules (Phase C.5 §5).
 *
 * Run with:  node --test src/hooks/captainRunOwnership.test.ts
 *
 * Node 20 executes a `.ts` file with no imports, no enums and no namespaces
 * after stripping the type annotations, so this file runs against the same
 * source the app bundles — no vitest, no jsdom, no new dependency. The module
 * under test is deliberately dependency-free (see its header) precisely so that
 * the rule guarding against mis-attributed judgements has a real assertion
 * behind it. The project has no component-test runner; this is the closest
 * honest thing to one, and it tests the part that is actually easy to get
 * wrong — the decision — rather than the React plumbing around it.
 *
 * The scenario that motivates every case here is the §5 bug: run A is on
 * screen, run B is requested, and A's late terminal event plus B's early quick
 * event interleave. Rendered naively, the user ends up looking at A's
 * provisional judgement pinned to B, or at A's finished report after asking
 * for B.
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  applyLoadedRun,
  claimRun,
  createCaptainRunOwnership,
  decideQuick,
  decideTerminal,
  isTerminalStatus,
  resetRun,
} from './captainRunOwnership.ts'

test('a quick for the run being shown is accepted', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  assert.equal(decideQuick(o, 'B'), true)
})

test('a quick for a different run is refused', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  // A's provisional result must not appear under B.
  assert.equal(decideQuick(o, 'A'), false)
})

test('a quick is refused once the run is known terminal', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  applyLoadedRun(o, 'B', 'COMPLETED')
  assert.equal(decideQuick(o, 'B'), false)
})

test('a quick is refused after a terminal event, before the GET returns', () => {
  // The load-bearing case: the terminal event settles the run, but `status` is
  // reset to null while the authoritative GET is in flight. Identity alone
  // would admit the quick here; the settled set is what refuses it.
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  decideTerminal(o, 'B')
  assert.equal(o.status, null)
  assert.equal(decideQuick(o, 'B'), false)
})

test("a late terminal for a previous run is ignored", () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'A')
  applyLoadedRun(o, 'A', 'ANALYZING')
  // The user asks for B.
  claimRun(o, 'B')
  // A finishes late.
  const d = decideTerminal(o, 'A')
  assert.deepEqual(d, { action: 'ignore' })
  // And B is untouched — its identity and liveness both survive.
  assert.equal(o.reportId, 'B')
  assert.equal(decideQuick(o, 'B'), true)
})

test('a terminal for the current run is loaded and settles it', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  const d = decideTerminal(o, 'B')
  assert.deepEqual(d, { action: 'load', reportId: 'B' })
  assert.equal(decideQuick(o, 'B'), false)
})

test('a terminal with no id falls back to latest without forgetting the run', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  const d = decideTerminal(o, null)
  assert.deepEqual(d, { action: 'load', reportId: null })
  // Identity is preserved: an unknown-id terminal may well BE run B.
  assert.equal(o.reportId, 'B')
})

test('claiming with no id does not erase the current identity', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  claimRun(o, null)
  assert.equal(o.reportId, 'B')
})

test('the §5 interleaving ends on B, not A', () => {
  // A on screen and finished; B requested and running.
  const o = createCaptainRunOwnership()
  claimRun(o, 'A')
  applyLoadedRun(o, 'A', 'COMPLETED')

  claimRun(o, 'B')
  // B's quick arrives first — accepted, this is the whole point of two stages.
  assert.equal(decideQuick(o, 'B'), true)
  // A's terminal event arrives late — ignored, must not clobber B.
  assert.equal(decideTerminal(o, 'A').action, 'ignore')
  // B is still the run on screen and still open to its own quick.
  assert.equal(o.reportId, 'B')
  assert.equal(decideQuick(o, 'B'), true)
  // B completes.
  assert.equal(decideTerminal(o, 'B').action, 'load')
  assert.equal(decideQuick(o, 'B'), false)
})

test('resetting a meeting forgets every run and its settled set', () => {
  const o = createCaptainRunOwnership()
  claimRun(o, 'B')
  applyLoadedRun(o, 'B', 'COMPLETED')
  resetRun(o)
  assert.equal(o.reportId, null)
  assert.equal(o.status, null)
  assert.equal(o.settled.size, 0)
  // A fresh run with the same id is accepted again — the settled set was the
  // only thing that would have refused it.
  claimRun(o, 'B')
  assert.equal(decideQuick(o, 'B'), true)
})

test('isTerminalStatus covers both terminal outcomes', () => {
  assert.equal(isTerminalStatus('COMPLETED'), true)
  assert.equal(isTerminalStatus('FAILED'), true)
  for (const live of ['PENDING', 'ANALYZING', null]) {
    assert.equal(isTerminalStatus(live), false)
  }
})
