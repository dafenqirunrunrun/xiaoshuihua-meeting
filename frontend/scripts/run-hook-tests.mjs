/**
 * Run the dependency-free frontend unit tests.
 *
 * This project has no component-test runner (no vitest/jest/jsdom) and adding
 * one for a handful of pure modules is not worth the dependency — but the
 * Captain ownership rules are exactly the kind of logic that must have an
 * executable check, so they get one without one. The tests are written against
 * Node's built-in `node:test`, and esbuild (already present as a Vite
 * dependency) bundles each file to plain JS that Node 20 can execute.
 *
 * Usage:  npm run test:hooks
 *         node scripts/run-hook-tests.mjs
 */
import { build } from 'esbuild'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

const root = process.cwd()
const out = mkdtempSync(path.join(tmpdir(), 'hook-tests-'))

// `readdirSync(..., {recursive:true})` (Node >= 18.17) rather than
// `fs.promises.glob`, which only exists from Node 22.
const files = readdirSync(path.join(root, 'src'), { recursive: true })
  .filter((f) => typeof f === 'string' && f.endsWith('.test.ts'))
  .map((f) => path.join('src', f))

if (files.length === 0) {
  console.error('no *.test.ts files found')
  process.exit(1)
}

const built = []
for (const [i, file] of files.entries()) {
  const outfile = path.join(out, `t${i}.mjs`)
  await build({
    entryPoints: [path.join(root, file)],
    outfile,
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    // `node:` builtins stay external; nothing else is imported by these tests.
    logLevel: 'warning',
  })
  built.push(outfile)
}

const result = spawnSync(process.execPath, ['--test', ...built], {
  stdio: 'inherit',
})
rmSync(out, { recursive: true, force: true })
process.exit(result.status ?? 1)
