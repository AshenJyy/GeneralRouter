import { createHash, randomUUID } from 'node:crypto'
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

const digest = value => createHash('sha256').update(value).digest('hex')
const identifier = value => typeof value === 'string' && /^[A-Za-z0-9:._-]{1,240}$/.test(value) ? value : undefined
const recordingActions = new Set(['record_usage', 'started', 'release'])

export const isRecordingAction = action => recordingActions.has(action)

function operationOf(envelope) {
  const record = envelope.record || {}
  const identity = envelope.action === 'record_usage' ? record.record_id
    : `${envelope.reservation_id || ''}:${envelope.action === 'release' ? envelope.outcome || '' : envelope.child_session_id || ''}`
  return {
    id: digest(`${envelope.action}:${identity}`),
    action: envelope.action,
    ...(identifier(envelope.task_id) ? { task_id: envelope.task_id } : {}),
    ...(identifier(record.record_id) ? { record_id: record.record_id } : {}),
    ...(identifier(record.session_id || envelope.child_session_id) ? { session_id: record.session_id || envelope.child_session_id } : {}),
    ...(identifier(envelope.reservation_id || record.reservation_id) ? { reservation_id: envelope.reservation_id || record.reservation_id } : {}),
    ...(identifier(envelope.outcome) ? { outcome: envelope.outcome } : {}),
  }
}

function failureOf(value) {
  // Bridge error messages are classified on the Python side. Never retain
  // raw stderr, request bodies, config values, prompts or arbitrary exceptions.
  if (value && !(value instanceof Error) && value.error?.code) {
    return {
      type: identifier(value.error.type) || 'BridgeError',
      code: identifier(value.error.code) || 'recording_rejected',
      message: typeof value.error.message === 'string' ? value.error.message.slice(0, 400) : 'The bridge rejected this recording operation.',
      ...(identifier(value.error.stage) ? { stage: value.error.stage } : {}),
      ...(Number.isInteger(value.error.errno) ? { errno: value.error.errno } : {}),
      ...(Number.isInteger(value.error.winerror) ? { winerror: value.error.winerror } : {}),
    }
  }
  if (value instanceof Error) {
    const message = value.message
    if (message === 'GeneralRouter operation cancelled') return { type: 'TransportError', code: 'cancelled', message }
    if (message === 'GeneralRouter bridge timed out') return { type: 'TransportError', code: 'timeout', message }
    if (message === 'GeneralRouter bridge response exceeded 2 MiB') return { type: 'TransportError', code: 'response_too_large', message }
    if (message === 'GeneralRouter bridge returned an invalid JSON object') return { type: 'TransportError', code: 'invalid_bridge_response', message }
    if (message === 'Cannot start the configured Python executable; check the local plugin settings') return { type: 'TransportError', code: 'python_start_failed', message }
    const exit = /^GeneralRouter bridge exited with code (-?\d+|null); diagnostic bytes: (\d+)$/.exec(message)
    if (exit) return { type: 'TransportError', code: 'bridge_exit', message: `Bridge exit code ${exit[1]}; stderr bytes ${exit[2]} (contents omitted).` }
    return { type: 'TransportError', code: 'bridge_transport_failed', message: 'The recording request could not reach or complete the local bridge.' }
  }
  return {
    type: 'RecordingRejected', code: identifier(value?.status) || 'recording_rejected',
    message: 'The bridge did not accept this recording operation. See the operation and record identifiers.',
  }
}

export class RecordingDiagnostics {
  constructor(projectRoot, runId) {
    this.runId = runId
    this.path = resolve(projectRoot, '.runs', 'generalrouter', 'diagnostics', `${digest(runId)}.json`)
    this.issues = new Map()
    this.pending = Promise.resolve()
    this.storageError = undefined
    this.loaded = this.load()
  }

  async load() {
    try {
      const saved = JSON.parse(await readFile(this.path, 'utf8'))
      if (saved.schema_version !== 'generalrouter-recording-diagnostics.v1' || saved.run_id !== this.runId || !Array.isArray(saved.issues)
        || saved.issues.some(issue => !issue || !/^[a-f0-9]{64}$/.test(issue.id)
          || !recordingActions.has(issue.action) || !['unresolved', 'resolved'].includes(issue.status)
          || typeof issue.last_failed_at !== 'string' || !Array.isArray(issue.occurrences))) {
        this.storageError = 'diagnostic_file_invalid'
        return
      }
      for (const issue of saved.issues) this.issues.set(issue.id, issue)
    } catch (error) {
      if (error.code !== 'ENOENT') this.storageError = 'diagnostic_file_unreadable'
    }
  }

  async persist() {
    // Preserve a file we could not load; do not overwrite unknown history.
    if (['diagnostic_file_invalid', 'diagnostic_file_unreadable'].includes(this.storageError)) return
    try {
      await mkdir(dirname(this.path), { recursive: true })
      const temporary = `${this.path}.${randomUUID()}.tmp`
      await writeFile(temporary, JSON.stringify({
        schema_version: 'generalrouter-recording-diagnostics.v1', run_id: this.runId,
        updated_at: new Date().toISOString(), issues: [...this.issues.values()],
      }, null, 2) + '\n', { encoding: 'utf8', mode: 0o600 })
      await rename(temporary, this.path)
      this.storageError = undefined
    } catch {
      this.storageError = 'diagnostic_write_failed'
    }
  }

  update(envelope, failure) {
    const operation = this.pending.then(async () => {
      await this.loaded
      const identity = operationOf(envelope)
      const previous = this.issues.get(identity.id)
      const at = new Date().toISOString()
      if (failure !== undefined) {
        const issue = previous || { ...identity, first_failed_at: at, occurrences: [] }
        issue.status = 'unresolved'
        issue.last_failed_at = at
        issue.resolved_at = null
        issue.last_error = failureOf(failure)
        issue.occurrences.push({ at, error: issue.last_error })
        this.issues.set(identity.id, issue)
      } else {
        // Only an accepted write for this same record/lifecycle operation
        // resolves it. Unrelated successful operations leave it untouched.
        if (!previous || previous.status !== 'unresolved') return
        previous.status = 'resolved'
        previous.resolved_at = at
      }
      await this.persist()
    })
    this.pending = operation.catch(() => { this.storageError = 'diagnostic_update_failed' })
    return this.pending
  }

  async summary(detail = false) {
    await this.loaded
    await this.pending
    const issues = [...this.issues.values()]
    const unresolved = issues.filter(issue => issue.status === 'unresolved')
    const latest = [...(unresolved.length ? unresolved : issues)].sort((a, b) => b.last_failed_at.localeCompare(a.last_failed_at))[0]
    return {
      status: this.storageError ? 'diagnostics_incomplete' : unresolved.length ? 'recording_issues' : 'no_known_recording_issues',
      unresolved_count: unresolved.length,
      resolved_count: issues.length - unresolved.length,
      reference: this.path,
      ...(this.storageError ? { storage_error: this.storageError } : {}),
      ...(latest ? { latest_issue: {
        id: latest.id, action: latest.action, task_id: latest.task_id,
        record_id: latest.record_id, reservation_id: latest.reservation_id,
        status: latest.status, first_failed_at: latest.first_failed_at,
        last_failed_at: latest.last_failed_at, resolved_at: latest.resolved_at,
        error: latest.last_error,
      } } : {}),
      ...(detail ? { issues } : {}),
      coverage: 'Failures observed by this plugin; no-known-issues does not establish complete provider usage coverage.',
    }
  }
}
