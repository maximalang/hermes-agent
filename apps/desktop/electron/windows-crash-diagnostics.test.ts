import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  enableWindowsCrashDiagnostics,
  windowsCrashDiagnostics,
  type WindowsCrashDiagnosticsHost
} from './windows-crash-diagnostics'

// Verified coverage gap from the 2026-10-22 incident diagnosis:
// crashReporter.start() was wired for Linux only, so on Windows no local
// minidumps were captured. The fix does not guess the crash cause; it closes
// the gap so the next failure is legible.

function recordingHost(overrides: Partial<WindowsCrashDiagnosticsHost> = {}) {
  const calls: Array<[name: string, arg?: unknown]> = []

  const host: WindowsCrashDiagnosticsHost = {
    ensureCrashesDir: (dir: string) => calls.push(['ensureCrashesDir', dir]),
    setCrashDumpsPath: (dir: string) => calls.push(['setCrashDumpsPath', dir]),
    startCrashReporter: (options: Parameters<WindowsCrashDiagnosticsHost['startCrashReporter']>[0]) =>
      calls.push(['startCrashReporter', options]),
    logStatus: (line: string) => calls.push(['logStatus', line]),
    ...overrides
  }

  return { host, calls, names: () => calls.map(([name]) => name) }
}

test('null plan (non-Windows platforms) is a complete no-op', () => {
  assert.equal(windowsCrashDiagnostics('/home/u/logs', { platform: 'linux' }), null)
  assert.equal(windowsCrashDiagnostics('/Users/u/logs', { platform: 'darwin' }), null)

  const { host, calls } = recordingHost()

  enableWindowsCrashDiagnostics(null, host)

  assert.deepEqual(calls, [])
})

test('on win32, dumps are local-only; the directory itself is created and used', () => {
  const logsDir = 'C:\\Users\\u\\AppData\\Roaming\\Hermes\\logs'
  const plan = windowsCrashDiagnostics(logsDir, { platform: 'win32', appVersion: '1.2.3' })

  assert.ok(plan)
  assert.equal(path.dirname(plan.crashesDirectory), logsDir)
  assert.equal(path.basename(plan.crashesDirectory), 'crash-dumps')
  assert.equal(plan.crashReporter.uploadToServer, false)
  assert.equal(plan.crashReporter.compress, false)
  assert.equal(plan.crashReporter.extra.appVersion, '1.2.3')

  const { host, calls } = recordingHost()

  enableWindowsCrashDiagnostics(plan, host)

  const ensured = calls.filter(([name]) => name === 'ensureCrashesDir')
  const directed = calls.filter(([name]) => name === 'setCrashDumpsPath')

  assert.equal(ensured.length, 1)
  assert.equal(ensured[0][1], plan.crashesDirectory)
  assert.equal(directed.length, 1)
  assert.equal(directed[0][1], plan.crashesDirectory)
})

test('happy path order: ensureCrashesDir -> setCrashDumpsPath -> startCrashReporter', () => {
  const plan = windowsCrashDiagnostics('C:\\logs', { platform: 'win32' })
  const { host, names } = recordingHost()

  enableWindowsCrashDiagnostics(plan, host)

  assert.deepEqual(names().slice(0, 3), ['ensureCrashesDir', 'setCrashDumpsPath', 'startCrashReporter'])
})

test('a setup step failing degrades to the default location, never kills startup', () => {
  for (const failing of ['ensureCrashesDir', 'setCrashDumpsPath'] as const) {
    const plan = windowsCrashDiagnostics('C:\\logs', { platform: 'win32' })

    const { host, calls, names } = recordingHost({
      [failing]: () => {
        throw new Error('EROFS: read-only file system')
      }
    } as Partial<WindowsCrashDiagnosticsHost>)

    enableWindowsCrashDiagnostics(plan, host)

    if (failing === 'ensureCrashesDir') {
      // setCrashDumpsPath must never run against a directory that was not created.
      assert.equal(names().includes('setCrashDumpsPath'), false, failing)
    }

    assert.equal(names().includes('startCrashReporter'), true, failing)

    const status = calls.filter(([name]) => name === 'logStatus').map(([, line]) => String(line))

    assert.ok(status.some(line => line.includes('default')), `${failing}: fallback must be reported`)
  }
})

test('a Crashpad that cannot start is a missing diagnostic, not a dead shell', () => {
  const plan = windowsCrashDiagnostics('C:\\logs', { platform: 'win32' })

  const { host, calls } = recordingHost({
    startCrashReporter: () => {
      throw new Error('Crashpad helper missing')
    }
  })

  enableWindowsCrashDiagnostics(plan, host)

  const status = calls.filter(([name]) => name === 'logStatus').map(([, line]) => String(line))

  assert.ok(status.some(line => line.includes('crashReporter.start failed')))
})

test('logStatus that throws never propagates', () => {
  const plan = windowsCrashDiagnostics('C:\\logs', { platform: 'win32' })

  const { host } = recordingHost({
    logStatus: () => {
      throw new Error('log sink closed')
    }
  })

  enableWindowsCrashDiagnostics(plan, host)
})

test('no app version means no empty extra annotation', () => {
  const plan = windowsCrashDiagnostics('C:\\logs', { platform: 'win32' })

  assert.ok(plan)
  assert.equal('appVersion' in plan.crashReporter.extra, false)
})
