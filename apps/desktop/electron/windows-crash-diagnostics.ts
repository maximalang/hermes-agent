import path from 'node:path'

// Coverage gap, not a crash diagnosis: on Windows crashReporter.start() was
// never wired (only the Linux diagnostics enabled it), so on Windows no local
// minidumps were captured — whatever the shell hit during the 2026-10-22
// incident left nothing to examine afterwards. This module enables Crashpad's
// local minidump database on Windows: dumps land next to desktop.log, nothing
// leaves the machine, and startup never depends on it. It does not claim what
// past failures were — it makes the next one legible.

export interface WindowsCrashDiagnostics {
  /** Directory for the local Crashpad database, next to desktop.log. */
  crashesDirectory: string
  /** `crashReporter.start` options; local database only, nothing leaves the machine. */
  crashReporter: { uploadToServer: false; compress: false; extra: Record<string, string> }
}

export function windowsCrashDiagnostics(
  logsDir: string,
  options: { platform?: NodeJS.Platform | string; appVersion?: string } = {}
): WindowsCrashDiagnostics | null {
  const platform = options.platform ?? process.platform

  if (platform !== 'win32') {
    return null
  }

  const extra: Record<string, string> = {}
  const appVersion = String(options.appVersion ?? '').trim()

  if (appVersion) {
    extra.appVersion = appVersion
  }

  return {
    crashesDirectory: path.join(logsDir, 'crash-dumps'),
    crashReporter: { uploadToServer: false, compress: false, extra }
  }
}

/** The side effects the plan needs, injected so the failure paths are provable. */
export interface WindowsCrashDiagnosticsHost {
  /** Create the crash-dumps directory itself. May throw (read-only or invalid HERMES_HOME). */
  ensureCrashesDir(dir: string): void
  /**
   * Point `crashDumps` at the plan's directory. Electron requires the
   * destination directory to exist, so this must run after ensureCrashesDir
   * and before startCrashReporter.
   */
  setCrashDumpsPath(dir: string): void
  startCrashReporter(options: WindowsCrashDiagnostics['crashReporter']): void
  /** Best-effort status line; must never throw (diagnostics must not break startup). */
  logStatus(line: string): void
}

// Diagnostics are optional; startup is not (same contract as the Linux path).
// Every step is best-effort and reports where dumps actually go: a read-only
// or invalid logs dir degrades to Crashpad's default location rather than a
// desktop that dies before app readiness. A silent success is not evidence a
// dump will be captured — only that the reporter started.
export function enableWindowsCrashDiagnostics(
  plan: WindowsCrashDiagnostics | null,
  host: WindowsCrashDiagnosticsHost
): void {
  const safeLog = (line: string) => {
    try {
      host.logStatus(line)
    } catch {
      // Status logging is best-effort by contract; never propagate.
    }
  }

  if (!plan) {
    return
  }

  let dumpsDirReady = true

  try {
    host.ensureCrashesDir(plan.crashesDirectory)
    host.setCrashDumpsPath(plan.crashesDirectory)
  } catch (error) {
    dumpsDirReady = false
    safeLog(
      `windows crash diagnostics: custom crash-dumps dir unavailable (${String(error)}); falling back to Crashpad default`
    )
  }

  try {
    host.startCrashReporter(plan.crashReporter)
    safeLog(
      dumpsDirReady
        ? `windows crash diagnostics: local Crashpad dumps -> ${plan.crashesDirectory}`
        : 'windows crash diagnostics: local Crashpad dumps -> default crashDumps location'
    )
  } catch (error) {
    // Crashpad unavailable — not a startup failure, but never silent.
    safeLog(`windows crash diagnostics: crashReporter.start failed (${String(error)}); no local dumps this run`)
  }
}
