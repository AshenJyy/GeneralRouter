import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, isAbsolute, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { lastAssistantStreamChunk } from '@deepseek-ai/dsh-llm'
import { isRecordingAction, RecordingDiagnostics } from './recording-diagnostics.mjs'

export const name = 'generalrouter'
export const inject = ['tools', 'systemPrompt', 'sessionProjections', 'skills']
export const Config = z.object({
  // Empty paths follow the Agent Session's original project, not the Host cwd.
  projectRoot: z.string().default(''),
  hostConfig: z.string().default(''),
  runtimeRoot: z.string().default('runtime'),
  pythonExecutable: z.string().default('py'),
  pythonArguments: z.array(z.string()).default(['-3']),
  bridgeTimeoutMs: z.natural().min(1000).max(120000).default(60000),
  delegationToolName: z.string().default('subagent'),
  activeQuotaRefreshSeconds: z.natural().min(600).max(86400).default(600),
})

const markerPattern = /\[generalrouter-reservation:([A-Za-z0-9._-]+)\]/
// DSH's bundled rank is 600; project and user skills have lower ranks and win.
const bundledSkillRank = 600
const skillName = 'generalrouter-task-profile'
const skillDescription = 'Prepare GeneralRouter subtasks with four-dimensional task weights estimated from authored task anchors. Preserve the distinction between planner estimates, recorded AA references and unknown weights.'
const jsonOutput = {
  schema: { type: 'json' },
  render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
}

function sharedHostConfig(ctx) {
  const homePath = ctx.get('dshHomePath')
  if (typeof homePath === 'function') return homePath('generalrouter', 'host.local.json')
  // Match dsh-home-paths when the boot-provided path helper is unavailable.
  const selected = process.env.DSH_HOME?.trim() ? process.env.DSH_HOME : resolve(homedir(), '.dsh')
  const expanded = selected === '~' ? homedir()
    : selected.startsWith('~/') || selected.startsWith('~\\') ? resolve(homedir(), selected.slice(2)) : selected
  return resolve(expanded, 'generalrouter', 'host.local.json')
}

function registerBundledSkill(ctx, runtimeRoot) {
  const resourceBase = { kind: 'directory', path: resolve(runtimeRoot, 'skills', skillName) }
  const path = resolve(resourceBase.path, 'SKILL.md')
  const summary = {
    path, name: skillName, description: skillDescription,
    invocation: { modelInvocable: true, userInvocable: true },
    provider: 'generalrouter', source: 'bundled', resourceBase,
  }
  const candidate = { ...summary, rank: bundledSkillRank, locator: path }
  // registerProvider owns disposal, so disabling/uninstalling removes the skill.
  ctx.skills.registerProvider(() => ({
    name: 'generalrouter',
    list: async (options = {}) => {
      options.signal?.throwIfAborted()
      return [candidate]
    },
    get: async (_candidate, options = {}) => {
      const raw = await readFile(path, { encoding: 'utf8', signal: options.signal })
      const content = raw.replace(/^\uFEFF?---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)/, '').trim()
      return { ...summary, content }
    },
  }))
}

function markerOf(value) {
  if (typeof value === 'string') return markerPattern.exec(value)?.[1]
  if (!Array.isArray(value)) return undefined
  for (const block of value) {
    if (block?.type === 'text') {
      const match = markerOf(block.text)
      if (match !== undefined) return match
    }
  }
  return undefined
}

function safeError(error) {
  // Do not echo subprocess stderr, the profile, prompts, or environment values.
  return error instanceof Error ? error.message : 'GeneralRouter bridge operation failed'
}

function canonicalRoute(route) {
  if (!route) return undefined
  const effort = route.reasoning_effort ?? route.reasoningEffort
  return {
    ...(typeof route.provider === 'string' ? { provider: route.provider } : {}),
    ...(typeof route.model === 'string' ? { model: route.model } : {}),
    ...(typeof effort === 'string' ? { reasoning_effort: effort } : {}),
  }
}

function callBridge(settings, envelope, signal) {
  return new Promise((accept, reject) => {
    if (signal?.aborted) return reject(new Error('GeneralRouter operation cancelled'))
    const child = spawn(settings.pythonExecutable,
      [...settings.pythonArguments, '-m', 'generalrouter.host_bridge'], {
        cwd: settings.runtimeRoot,
        shell: false,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      })
    let stdout = ''
    let stderrBytes = 0
    let failure
    let helper
    const stop = (message) => {
      if (failure !== undefined) return
      failure = new Error(message)
      if (!child.pid) return
      if (process.platform === 'win32') {
        // py.exe can own a Python child; stopping only the launcher leaks work.
        helper = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], {
          shell: false, windowsHide: true, stdio: 'ignore',
        })
        helper.on('error', () => { child.kill() })
      } else child.kill('SIGTERM')
    }
    const abort = () => stop('GeneralRouter operation cancelled')
    const timer = setTimeout(() => stop('GeneralRouter bridge timed out'), settings.bridgeTimeoutMs)
    signal?.addEventListener('abort', abort, { once: true })
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', chunk => {
      stdout += chunk
      if (Buffer.byteLength(stdout, 'utf8') > 2 * 1024 * 1024) stop('GeneralRouter bridge response exceeded 2 MiB')
    })
    child.stderr.on('data', chunk => { stderrBytes += chunk.length })
    child.stdin.on('error', () => {})
    child.on('error', () => {
      failure ??= new Error('Cannot start the configured Python executable; check the local plugin settings')
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', abort)
      if (failure !== undefined) return reject(failure)
      if (code !== 0) return reject(new Error(`GeneralRouter bridge exited with code ${code}; diagnostic bytes: ${stderrBytes}`))
      try {
        const value = JSON.parse(stdout.replace(/^\uFEFF/, '').trim())
        if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('object required')
        accept(value)
      } catch {
        reject(new Error('GeneralRouter bridge returned an invalid JSON object'))
      }
    })
    child.stdin.end(JSON.stringify({ ...envelope, project_root: settings.projectRoot,
      user_config_file: settings.userConfigFile, config_file: settings.hostConfig,
      config_explicit: settings.configExplicit,
      runtime_root: settings.runtimeRoot }) + '\n', 'utf8')
  })
}

export function apply(ctx, config = {}) {
  const pluginRoot = dirname(fileURLToPath(import.meta.url))
  const settings = {
    runtimeRoot: resolve(pluginRoot, config.runtimeRoot || 'runtime'),
    userConfigFile: sharedHostConfig(ctx),
    pythonExecutable: config.pythonExecutable || 'py',
    pythonArguments: config.pythonArguments || ['-3'],
    bridgeTimeoutMs: config.bridgeTimeoutMs || 60000,
    delegationToolName: config.delegationToolName || 'subagent',
    activeQuotaRefreshSeconds: Math.max(600, config.activeQuotaRefreshSeconds || 600),
  }
  registerBundledSkill(ctx, settings.runtimeRoot)
  const lifecycle = new AbortController()
  const quotaLifecycle = new AbortController()
  const sessionStates = new Map()
  const reservations = new Map()
  const roots = new Map()
  const runSettings = new Map()
  const authorizedCalls = new Set()
  let queue = Promise.resolve()
  const recordingDiagnostics = new Map()
  let quotaRefreshTimer
  const pendingPreparations = new Map()
  let quotaRefreshInFlight
  let quotaRefreshDiagnostic
  const stateFor = session => {
    let state = sessionStates.get(session.id)
    if (state === undefined) {
      state = {
        session,
        parentId: session.header.parentSession,
        route: undefined,
        attempts: new Map(),
        records: new Map(),
        rootId: undefined,
        reservationId: undefined,
      }
      sessionStates.set(session.id, state)
    }
    return state
  }
  const rootFor = state => {
    if (state.rootId) return state.rootId
    const seen = new Set()
    let current = state
    while (current && !seen.has(current.session.id)) {
      seen.add(current.session.id)
      if (roots.has(current.session.id)) return current.session.id
      current = current.session.header.origin === 'subagent' ? sessionStates.get(current.parentId) : undefined
    }
    return undefined
  }
  const bindProject = session => {
    const runId = rootFor(stateFor(session)) || session.id
    if (runSettings.has(runId)) return runSettings.get(runId)
    const rootSession = sessionStates.get(runId)?.session || session
    const projectRoot = config.projectRoot?.trim() || rootSession.header.cwd
      || ctx.get('workingDirectory')?.get(rootSession)
    if (typeof projectRoot !== 'string' || !isAbsolute(projectRoot)) {
      throw new Error('Open the project folder in DSH, or set an absolute GeneralRouter projectRoot override.')
    }
    const bound = {
      ...settings,
      projectRoot: resolve(projectRoot),
      hostConfig: resolve(projectRoot, config.hostConfig?.trim() || 'host.local.json'),
      configExplicit: Boolean(config.hostConfig?.trim()),
    }
    // Freeze the paths for this run; later cd operations and child workspaces
    // cannot move its configuration, quota cache or usage records.
    runSettings.set(runId, bound)
    return bound
  }
  const prepareProject = agent => {
    try { bindProject(agent.session) } catch (error) {
      return { ok: false, status: 'setup_required', message: safeError(error) }
    }
  }
  const diagnosticsFor = runId => {
    let diagnostics = recordingDiagnostics.get(runId)
    if (!diagnostics) {
      const bound = runSettings.get(runId)
      if (!bound) return undefined
      diagnostics = new RecordingDiagnostics(bound.projectRoot, runId)
      recordingDiagnostics.set(runId, diagnostics)
    }
    return diagnostics
  }
  const bridge = async (envelope, signal) => {
    const bound = runSettings.get(envelope.run_id)
    if (!bound) throw new Error('GeneralRouter run has no bound project directory.')
    const diagnostics = isRecordingAction(envelope.action) ? diagnosticsFor(envelope.run_id) : undefined
    const reservation = reservations.get(envelope.reservation_id || envelope.record?.reservation_id)
    const identified = { ...envelope, ...(reservation?.taskId ? { task_id: reservation.taskId } : {}) }
    let result
    try {
      result = await callBridge(bound, envelope, signal)
    } catch (error) {
      await diagnostics?.update(identified, error)
      if (diagnostics) ctx.logger.warn(`generalrouter: ${envelope.action} recording transport failed for run ${envelope.run_id}; see recording_diagnostics`)
      throw error
    }
    if (diagnostics) {
      await diagnostics.update(identified, result.ok === true ? undefined : result)
      if (result.ok !== true) ctx.logger.warn(`generalrouter: ${envelope.action} recording rejected for run ${envelope.run_id}; see recording_diagnostics`)
    }
    return result
  }
  const withDiagnostics = async (result, runId, detail = false, includeHealthy = true) => {
    const diagnostics = diagnosticsFor(runId)
    if (!diagnostics) return result
    const summary = await diagnostics.summary(detail)
    if (!includeHealthy && !detail && !summary.unresolved_count && !summary.storage_error) return result
    return {
      ...result, recording_diagnostics: summary,
      ...(summary.unresolved_count || summary.storage_error ? {
        recording_warning: summary.storage_error || `${summary.unresolved_count} unresolved recording operation(s); see recording_diagnostics`,
      } : {}),
    }
  }
  const toolBridge = async (envelope, signal) => {
    try { return await bridge(envelope, signal) } catch (error) {
      if (signal?.aborted) throw error
      // Keep the independent recorder diagnostics available even when Python
      // itself cannot run. This is a failure result, never an empty report.
      return { ok: false, status: 'bridge_unavailable', action: envelope.action, message: safeError(error) }
    }
  }
  const enqueue = envelope => {
    const operation = queue.then(() => bridge(envelope, lifecycle.signal))
    queue = operation.then(() => undefined, () => undefined)
    return operation
  }
  const writeRecord = (state, record) => {
    const rootId = rootFor(state)
    if (!rootId) return
    state.rootId = rootId
    const reservationId = state.reservationId
    const reservation = reservationId ? reservations.get(reservationId) : undefined
    void enqueue({
      action: 'record_usage', run_id: rootId,
      record: {
        ...record,
        role: state.session.id === rootId ? 'planner' : 'worker',
        ...(reservationId ? { reservation_id: reservationId } : {}),
        ...(reservation?.taskId ? { task_id: reservation.taskId } : {}),
        ...(reservation?.configurationId ? { configuration_id: reservation.configurationId } : {}),
      },
    }).catch(() => {})
  }
  const flushState = state => {
    for (const record of state.records.values()) writeRecord(state, record)
  }
  const bindChild = (reservationId, childSessionId, actualRoute) => {
    const reservation = reservations.get(reservationId)
    if (!reservation) return
    reservation.childId = childSessionId
    const state = sessionStates.get(childSessionId)
    if (state) {
      state.rootId = reservation.rootId
      state.reservationId = reservationId
      flushState(state)
    }
    void enqueue({
      action: 'started', run_id: reservation.rootId,
      reservation_id: reservationId, child_session_id: childSessionId,
      ...(actualRoute ? { actual_route: canonicalRoute(actualRoute) } : {}),
    }).catch(() => {})
    if (reservation.end) {
      void enqueue({ action: 'release', run_id: reservation.rootId,
        reservation_id: reservationId, outcome: reservation.end }).catch(() => {})
    }
  }

  const activeQuotaRuns = () => new Set([
    ...pendingPreparations.keys(),
    ...[...reservations.values()].filter(entry => !entry.end).map(entry => entry.rootId),
  ])
  const hasActiveQuotaWork = () => activeQuotaRuns().size > 0
  const reconcileQuotaTimer = ({ restart = false } = {}) => {
    const active = !lifecycle.signal.aborted && !quotaLifecycle.signal.aborted && hasActiveQuotaWork()
    if ((!active || restart) && quotaRefreshTimer !== undefined) {
      clearTimeout(quotaRefreshTimer)
      quotaRefreshTimer = undefined
    }
    if (!active || quotaRefreshTimer !== undefined || quotaRefreshInFlight) return
    quotaRefreshTimer = setTimeout(() => {
      quotaRefreshTimer = undefined
      refreshActiveQuota()
    }, settings.activeQuotaRefreshSeconds * 1000)
    quotaRefreshTimer.unref?.()
  }
  const refreshActiveQuota = () => {
    if (lifecycle.signal.aborted || quotaLifecycle.signal.aborted || quotaRefreshInFlight || !hasActiveQuotaWork()) return
    // Initial matches and fixed-route repairs obtain a cold/stale snapshot through the bridge. This
    // timer exists only while work is pending or running; idle has no polling.
    const projects = new Map()
    for (const runId of activeQuotaRuns()) {
      const bound = runSettings.get(runId)
      if (!bound) continue
      const key = JSON.stringify([bound.projectRoot, bound.hostConfig, bound.userConfigFile])
      if (!projects.has(key)) projects.set(key, runId)
    }
    const operation = Promise.allSettled([...projects.values()].map(runId => bridge({
      action: 'refresh_quota', run_id: runId,
    }, quotaLifecycle.signal)))
    quotaRefreshInFlight = operation
    void operation.then(results => {
      const diagnostics = results.map(result => result.status === 'rejected' ? safeError(result.reason)
        : result.value.ok === false ? result.value.status || 'quota_refresh_failed' : undefined).filter(Boolean)
      const diagnostic = [...new Set(diagnostics)].join('; ') || undefined
      if (diagnostic && diagnostic !== quotaRefreshDiagnostic) {
        ctx.logger.warn(`generalrouter: background quota refresh unavailable (${diagnostic})`)
      }
      quotaRefreshDiagnostic = diagnostic
    }, error => {
      if (lifecycle.signal.aborted || quotaLifecycle.signal.aborted) return
      const diagnostic = safeError(error)
      if (diagnostic !== quotaRefreshDiagnostic) {
        ctx.logger.warn(`generalrouter: background quota refresh unavailable (${diagnostic})`)
      }
      quotaRefreshDiagnostic = diagnostic
    }).finally(() => {
      if (quotaRefreshInFlight === operation) quotaRefreshInFlight = undefined
      reconcileQuotaTimer()
    })
  }

  const dispatchContext = exec => {
    if (!exec.agent) return undefined
    const state = stateFor(exec.agent.session)
    const rootId = rootFor(state)
    if (!rootId) return undefined
    if (exec.name === 'send_message' && exec.agent.session.id === rootId) {
      const targetId = exec.arguments?.agent_id
      const target = typeof targetId === 'string' ? sessionStates.get(targetId) : undefined
      const ended = target?.ended || [...reservations.values()].some(entry => entry.childId === targetId && entry.end)
      if (ended) return {
        denial: 'This worker already ended. Report its result to the parent. If the parent chooses repair, use generalrouter_prepare_repair with a new task_id and a concise memento, then dispatch a fresh worker on the original fixed route. Do not match again or reopen this worker.',
      }
      return undefined
    }
    const isNativeDispatch = exec.name === settings.delegationToolName
    const isAlternateDispatch = ['subagent_fork', 'workflow', 'subagent_codex', 'subagent_antigravity'].includes(exec.name)
    if (!isNativeDispatch && !isAlternateDispatch) return undefined
    if (exec.agent.session.id !== rootId) return {
      denial: 'GeneralRouter v0.1 workers cannot start further workers. Return the new subtask to the parent planner.',
    }
    if (!isNativeDispatch) return {
      denial: `Use the reserved native ${settings.delegationToolName} route for this GeneralRouter run; ${exec.name} has no GeneralRouter reservation.`,
    }
    const reservationId = markerOf(exec.arguments?.prompt)
    const reservation = reservationId ? reservations.get(reservationId) : undefined
    if (!reservation || reservation.rootId !== rootId) return {
      denial: 'This GeneralRouter run requires a reservation marker returned by generalrouter_match for an independent task, or generalrouter_prepare_repair for a same-model repair, before dispatch.',
    }
    return { rootId, reservationId, reservation }
  }

  ctx.on('tools/pre-execute', async (exec, next) => {
    const dispatch = dispatchContext(exec)
    if (!dispatch) return next()
    if (dispatch.denial) return { kind: 'deny', reason: dispatch.denial }
    await queue
    let result
    try {
      result = await bridge({
        action: 'authorize_dispatch', run_id: dispatch.rootId,
        reservation_id: dispatch.reservationId, call_id: exec.callId,
        actual_route: {
          provider: exec.arguments.provider ?? null,
          model: exec.arguments.model ?? null,
          ...(exec.arguments.reasoning_effort ? { reasoning_effort: exec.arguments.reasoning_effort } : {}),
        },
      }, exec.signal)
    } catch (error) {
      return exec.signal.aborted ? { kind: 'cancel' } : { kind: 'deny', reason: safeError(error) }
    }
    if (result.ok !== true) return {
      kind: 'deny', reason: `GeneralRouter refused dispatch (${result.status || 'not_authorized'}). Report the blocked route to the parent. A never-started dispatch may be prepared again using ${dispatch.reservation.preparationTool || 'the original preparation tool'} with its fixed route; do not bypass the reservation, re-rank a repair, or substitute a model.`,
    }
    authorizedCalls.add(exec.token)
    return next()
  })
  // This registry guard runs after every mutable policy waterfall. Even an
  // unrelated policy returning allow cannot undo GeneralRouter's refusal.
  ctx.tools.guard(exec => {
    const dispatch = dispatchContext(exec)
    if (!dispatch) return undefined
    if (dispatch.denial) return dispatch.denial
    if (!authorizedCalls.has(exec.token)) return 'GeneralRouter did not authorize this exact native dispatch call.'
    return undefined
  })

  // rc.2's durable session settlements include failed/retried model attempts.
  // Observe only future, child-owned events; inherited fork prefixes are not billed again.
  ctx.on('session/event', (session, event) => {
    if (!session.isOwnSeq(event.seq)) return
    const state = stateFor(session)
    if (event.type === 'request/header') state.route = { ...event.data.header.config }
    if (event.type === 'llm/retry-started') {
      const key = `${event.data.turn}:${event.data.step}`
      state.attempts.set(key, (state.attempts.get(key) || 0) + 1)
    }
    if (event.type === 'user/message') {
      const reservationId = markerOf(event.data.message?.content)
      if (reservationId && reservations.has(reservationId)) bindChild(reservationId, session.id, state.route)
    }
    if (event.type !== 'assistant/message' && event.type !== 'assistant/attempt') return
    const usage = event.data.usage || lastAssistantStreamChunk(event.data.stream || [], 'usage')?.usage
    const key = `${event.data.turn}:${event.data.step}`
    const attempt = state.attempts.get(key) || 0
    const route = state.route || session.requestHeader()?.config
    const record = {
      record_id: `dsh:${session.id}:${key}:${attempt}`,
      session_id: session.id,
      turn: event.data.turn, step: event.data.step, attempt,
      event_seq: event.seq,
      observed_at: new Date(event.time).toISOString(),
      provider: route?.provider || null,
      model: route?.model || null,
      ...(route?.reasoningEffort ? { reasoning_effort: route.reasoningEffort } : {}),
      usage_reported: usage !== undefined,
      tokens: {
        uncached_input: usage?.inputTokens ?? null,
        cached_input: usage === undefined ? null : usage.cacheReadTokens ?? 0,
        cache_write_input: usage === undefined ? null : usage.cacheWriteTokens ?? 0,
        output: usage?.outputTokens ?? null,
        reasoning: usage?.reasoningTokens ?? null,
      },
      reasoning_included_in_output: true,
      source: 'dsh-session-provider-usage',
    }
    state.records.set(record.record_id, record)
    writeRecord(state, record)
  })

  // The immutable native tool result contains the child identity. No native
  // tool arguments, model routes or permission decisions are rewritten here.
  ctx.on('tools/result', (exec, result) => {
    authorizedCalls.delete(exec.token)
    if (exec.name !== settings.delegationToolName || !exec.agent) return
    const reservationId = markerOf(exec.arguments?.prompt)
    if (!reservationId || !reservations.has(reservationId)) return
    if (result.isError) {
      const reservation = reservations.get(reservationId)
      reservation.end = reservation.childId ? 'failed' : 'not_dispatched'
      reconcileQuotaTimer()
      void enqueue({ action: 'release', run_id: reservation.rootId,
        reservation_id: reservationId,
        outcome: reservation.end }).catch(() => {})
      return
    }
    const childId = result.value?.subagentId
    if (typeof childId === 'string') {
      const actualRoute = sessionStates.get(childId)?.route
      bindChild(reservationId, childId, actualRoute || {
        ...(typeof exec.arguments.provider === 'string' ? { provider: exec.arguments.provider } : {}),
        ...(typeof exec.arguments.model === 'string' ? { model: exec.arguments.model } : {}),
        ...(exec.arguments.reasoning_effort ? { reasoning_effort: exec.arguments.reasoning_effort } : {}),
      })
    }
  })
  ctx.on('subagent/end', info => {
    const state = sessionStates.get(info.id)
    const reservation = [...reservations.entries()].find(([, entry]) => entry.childId === info.id)
    if (state) {
      state.ended = true
      flushState(state)
    }
    if (!reservation) return
    const [reservationId, entry] = reservation
    entry.end = info.stopReason === 'completed' ? 'completed'
      : info.stopReason === 'aborted' ? 'cancelled' : 'failed'
    reconcileQuotaTimer()
    void enqueue({ action: 'release', run_id: entry.rootId,
      reservation_id: reservationId, outcome: entry.end }).catch(() => {})
  })
  ctx.on('session/flush', async session => {
    if (rootFor(stateFor(session))) await queue
  })
  ctx.on('dispose', async () => {
    clearTimeout(quotaRefreshTimer)
    quotaRefreshTimer = undefined
    // Cancel only the read-only background request. The recorder queue reaches
    // its normal settlement before the plugin's lifecycle signal is aborted.
    quotaLifecycle.abort()
    if (quotaRefreshInFlight) await quotaRefreshInFlight.catch(() => {})
    await queue
    lifecycle.abort()
  })

  const hostInfo = agent => ({
    name: 'dsh', version: '0.2.1-rc.2',
    agent_id: agent.id, session_id: agent.session.id,
    cwd: ctx.get('workingDirectory')?.get(agent.session) || agent.session.header.cwd || bindProject(agent.session).projectRoot,
    project_root: bindProject(agent.session).projectRoot,
    observation_scope: 'new-events-since-plugin-load',
  })
  const currentRun = agent => rootFor(stateFor(agent.session)) || agent.session.id
  const modelMetadataFor = async (agent, signal) => {
    let projectedModels
    try {
      projectedModels = ctx.sessionProjections.stateOf(agent.session, 'subagentModelSelectionPolicy')
    } catch { /* This optional projection supplies metadata only, never Router admission. */ }
    if (!Array.isArray(projectedModels) || projectedModels.length === 0) return []
    const llm = ctx.get('llm')
    const metadata = []
    for (const route of projectedModels) {
      if (typeof route?.provider !== 'string' || typeof route?.model !== 'string') continue
      const detached = { provider: route.provider, model: route.model }
      if (llm) {
        try {
          const info = await llm.resolveModelInfo(route.provider, route.model, signal)
          detached.reasoning_efforts = info.reasoning?.efforts.map(effort => effort.id) || []
          if (info.reasoning?.defaultEffort) detached.default_reasoning_effort = info.reasoning.defaultEffort
        } catch { detached.metadata_status = 'unavailable_use_default_effort' }
      }
      metadata.push(detached)
    }
    return metadata
  }
  const rememberPreparedDispatches = (result, runId, preparationTool) => {
    const tasks = Array.isArray(result.tasks) ? result.tasks : result.reservation_id ? [result] : []
    for (const task of tasks) {
      if (!task.reservation_id || !task.dispatch) continue
      reservations.set(task.reservation_id, { rootId: runId, taskId: task.task_id,
        configurationId: task.configuration_id, dispatch: task.dispatch, preparationTool })
    }
  }
  const beginPreparation = runId => {
    pendingPreparations.set(runId, (pendingPreparations.get(runId) || 0) + 1)
    reconcileQuotaTimer()
  }
  const endPreparation = runId => {
    const remaining = (pendingPreparations.get(runId) || 1) - 1
    if (remaining > 0) pendingPreparations.set(runId, remaining)
    else pendingPreparations.delete(runId)
    reconcileQuotaTimer()
  }

  ctx.tools.register(defineTool({
    name: 'generalrouter_match',
    description: 'Select the model for one ready independent task and return exact native subagent dispatch arguments. Router checks mapping, quota and tickets, without filtering by the DSH native model allowlist; DSH may still reject the actual invocation. Its provider/model/effort are fixed after first selection. Submit task-profile.v3 once; reuse profile_id. Repairs use generalrouter_prepare_repair and inherit that fixed route without matching. Ended tasks never reopen. Tickets prevent duplicate dispatch without deducting quota. This tool does not call a model or fall back automatically after a host rejection.',
    parameters: {
      profile_json: { type: 'string', description: 'First call: complete task-profile.v3 JSON from the skill. Later calls may omit it and use profile_id. Repeated full profiles remain supported.' },
      profile_id: { type: 'string', description: 'Exact identifier returned by the first match in this run. Reuses the saved plan and original request.' },
      original_request: { type: 'string', description: 'Original request and constraints for the first call. Omit on later calls to reuse the saved value.' },
      task_id: { type: 'string', description: 'Optional task id from this profile to select a particular ready task.' },
      retry_failed: { type: 'boolean', description: 'Legacy parameter; cannot reopen an ended task. The parent may use generalrouter_prepare_repair for a fresh same-model repair worker. A never-started independent task may be prepared again with its fixed route.' },
      detail: { type: 'boolean', description: 'Include full candidate scoring and task explanation. Default false; dispatch is always complete.' },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_match requires an Agent Session')
      if (exec.agent.session.header.origin === 'subagent') return {
        ok: false, status: 'delegate_to_parent', message: 'The parent planner owns GeneralRouter matching in v0.1.',
      }
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      let profile
      if (args.profile_json !== undefined) {
        try { profile = JSON.parse(args.profile_json) } catch { return { ok: false, status: 'invalid_profile_json' } }
        if (!profile || typeof profile !== 'object' || Array.isArray(profile)) return { ok: false, status: 'invalid_profile_json' }
      }
      if (!profile && !args.profile_id) return {
        ok: false, status: 'profile_required', message: 'Submit profile_json once, or pass the profile_id previously returned in this run.',
      }
      const modelMetadata = await modelMetadataFor(exec.agent, exec.signal)
      const runId = exec.agent.session.id
      roots.set(runId, true)
      const state = stateFor(exec.agent.session)
      state.rootId = runId
      flushState(state)
      await queue
      beginPreparation(runId)
      let result
      try {
        result = await toolBridge({
          action: 'match', run_id: runId,
          ...(profile ? { profile } : {}),
          ...(args.profile_id !== undefined ? { profile_id: args.profile_id } : {}),
          ...(args.original_request !== undefined ? { original_request: args.original_request } : {}),
          detail: args.detail === true,
          ...(args.task_id ? { task_id: args.task_id } : {}),
          ...(args.retry_failed === true ? { retry_failed: true } : {}),
          host: { ...hostInfo(exec.agent), call_id: exec.callId, model_metadata: modelMetadata },
          model_metadata: modelMetadata,
        }, exec.signal)
        rememberPreparedDispatches(result, runId, 'generalrouter_match')
      } finally {
        endPreparation(runId)
      }
      return withDiagnostics(result, runId, args.detail === true, false)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'generalrouter_prepare_repair',
    description: 'Prepare a fresh native subagent for a repair chosen by the parent after an earlier task ended. Inherit the exact frozen provider/model/effort regardless of optional metadata; do not compute a new task vector, score candidates, or select another model. Supply a concise memento of attempted strategies, failures, evidence and next steps. Returns dispatch arguments and a ticket after mapping/quota admission checks, without a native allowlist prefilter. DSH may reject the actual invocation; report that failure without automatic fallback. This tool does not start a worker or loop repairs.',
    parameters: {
      profile_id: { type: 'string', required: true, description: 'Saved profile_id from this run; the original profile stays unchanged.' },
      repairs_task_id: { type: 'string', required: true, description: 'Ended task being repaired. Its frozen provider/model/effort are inherited by the new repair task.' },
      task_id: { type: 'string', required: true, description: 'New unique repair task ID. Reuse this ID only to repeat the identical preparation before dispatch; an ended repair also needs a fresh task ID and worker.' },
      objective: { type: 'string', required: true, description: 'Specific repair goal and completion requirements chosen by the parent.' },
      memento: { type: 'string', required: true, description: 'Nonempty concise handoff: attempted strategies, what failed, evidence or artifact references, and the proposed next step. Distinguish observations from guesses; never claim an unperformed check passed.' },
      input_artifacts: { type: 'array', items: { type: 'string' }, description: 'Optional artifact paths or references needed by the fresh worker. Include only relevant inputs.' },
      detail: { type: 'boolean', description: 'Include full repair preparation details. Default false; dispatch is always complete.' },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_prepare_repair requires an Agent Session')
      if (exec.agent.session.header.origin === 'subagent') return {
        ok: false, status: 'delegate_to_parent', message: 'Report the failure and a concise memento to the parent; only the parent decides whether to prepare a same-model repair worker.',
      }
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      const modelMetadata = await modelMetadataFor(exec.agent, exec.signal)
      const runId = exec.agent.session.id
      roots.set(runId, true)
      const state = stateFor(exec.agent.session)
      state.rootId = runId
      flushState(state)
      await queue
      beginPreparation(runId)
      let result
      try {
        result = await toolBridge({
          action: 'prepare_repair', run_id: runId,
          profile_id: args.profile_id, repairs_task_id: args.repairs_task_id,
          task_id: args.task_id, objective: args.objective, memento: args.memento,
          ...(args.input_artifacts !== undefined ? { input_artifacts: args.input_artifacts } : {}),
          detail: args.detail === true,
          host: { ...hostInfo(exec.agent), call_id: exec.callId, model_metadata: modelMetadata },
          model_metadata: modelMetadata,
        }, exec.signal)
        rememberPreparedDispatches(result, runId, 'generalrouter_prepare_repair')
      } finally {
        endPreparation(runId)
      }
      return withDiagnostics(result, runId, args.detail === true, false)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'generalrouter_append_task',
    description: 'Append one additional independent task without changing the original plan or selecting a model. Then call generalrouter_match with profile_id and its new task_id. Identical additions are idempotent. For repair work, use generalrouter_prepare_repair instead; repairs inherit their original route and never enter new matching.',
    parameters: {
      profile_id: { type: 'string', required: true, description: 'Saved profile_id from this run; remains unchanged after additions.' },
      task_json: { type: 'string', required: true, description: 'One complete independent task object using the existing task-profile.v3 task fields, not a whole profile. Include specific input artifacts and acceptance requirements in context/deliverable.' },
      repairs_task_id: { type: 'string', description: 'Legacy field retained for compatibility only. Repair additions here are rejected with guidance to generalrouter_prepare_repair; do not use this tool to route a repair.' },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_append_task requires an Agent Session')
      if (exec.agent.session.header.origin === 'subagent') return {
        ok: false, status: 'delegate_to_parent', message: 'The parent planner owns task additions.',
      }
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      let task
      try { task = JSON.parse(args.task_json) } catch { return { ok: false, status: 'invalid_task_json' } }
      if (!task || typeof task !== 'object' || Array.isArray(task)) return { ok: false, status: 'invalid_task_json' }
      const runId = exec.agent.session.id
      roots.set(runId, true)
      const state = stateFor(exec.agent.session)
      state.rootId = runId
      flushState(state)
      await queue
      const result = await toolBridge({ action: 'append_task', run_id: runId,
        profile_id: args.profile_id, task,
        ...(args.repairs_task_id !== undefined ? { repairs_task_id: args.repairs_task_id } : {}),
        host: hostInfo(exec.agent) }, exec.signal)
      return withDiagnostics(result, runId, false, false)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'generalrouter_record_acceptance',
    description: 'Record the parent’s acceptance result for an ended task, separately from worker completion. This records supplied evidence; it does not run tests or verify claims. Later corrections preserve history. A successful repair does not rewrite the original task’s failed result.',
    parameters: {
      task_id: { type: 'string', required: true },
      outcome: { type: 'string', required: true, enum: ['unknown', 'passed', 'failed'] },
      summary: { type: 'string', required: true, description: 'Concise result and scope; distinguish child-reported results from checks actually performed. Include remaining failures or unverified requirements.' },
      evidence: { type: 'array', required: true, items: { type: 'string' }, description: 'Artifact/log references or concise expected-versus-actual observations. Use an empty list when no evidence is available and outcome unknown.' },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_record_acceptance requires an Agent Session')
      if (exec.agent.session.header.origin === 'subagent') return {
        ok: false, status: 'delegate_to_parent', message: 'Return acceptance evidence to the parent planner.',
      }
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      const runId = exec.agent.session.id
      roots.set(runId, true)
      const state = stateFor(exec.agent.session)
      state.rootId = runId
      flushState(state)
      await queue
      const result = await toolBridge({ action: 'record_acceptance', run_id: runId,
        task_id: args.task_id, outcome: args.outcome, summary: args.summary,
        evidence: args.evidence, host: hostInfo(exec.agent) }, exec.signal)
      return withDiagnostics(result, runId, false, false)
    },
  }))

  ctx.tools.register(defineTool({
    name: 'generalrouter_status',
    description: 'Read concise task progress, active tickets, cached quota and recording diagnostics. Use only when status is needed; normal completion is recorded automatically.',
    parameters: {
      refresh: { type: 'boolean', description: 'Explicit live quota refresh; normally use the ten-minute cache.' },
      detail: { type: 'boolean', description: 'Include configuration and full run details. Default false.' },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_status requires an Agent Session')
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      await queue
      const runId = currentRun(exec.agent)
      const result = await toolBridge({ action: 'status', run_id: runId,
        refresh: args.refresh === true, detail: args.detail === true, host: hostInfo(exec.agent) }, exec.signal)
      if (args.refresh === true) reconcileQuotaTimer({ restart: true })
      return withDiagnostics(result, runId, args.detail === true)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'generalrouter_release',
    description: 'Close a GeneralRouter dispatch ticket when a selected task was cancelled or never started. Started native subagent tickets close automatically from their actual completion event. Closing a ticket does not return quota. Do not mark an unfinished task completed.',
    parameters: {
      reservation_id: { type: 'string', required: true },
      outcome: { type: 'string', required: true, enum: ['cancelled', 'failed', 'not_dispatched', 'cancelled_before_dispatch'] },
    },
    output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_release requires an Agent Session')
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      await queue
      const result = await toolBridge({ action: 'release', run_id: currentRun(exec.agent),
        reservation_id: args.reservation_id, outcome: args.outcome }, exec.signal)
      if (result.ok === true && reservations.has(args.reservation_id)) {
        reservations.get(args.reservation_id).end = args.outcome
        reconcileQuotaTimer()
      }
      return withDiagnostics(result, currentRun(exec.agent))
    },
  }))
  ctx.tools.register(defineTool({
    name: 'generalrouter_report',
    description: 'Read totals, planner/worker/task usage, linked repair-group costs, execution and acceptance results, API-equivalent cost and observed timing. Group totals overlap task totals; never add them together. Missing or ambiguous attribution remains unknown. Normally request once at delivery; detail includes full records.',
    parameters: { detail: { type: 'boolean', description: 'Include individual usage records, task records and events. Default false.' } }, output: jsonOutput,
    async execute(args, exec) {
      if (!exec.agent) throw new Error('generalrouter_report requires an Agent Session')
      const setupError = prepareProject(exec.agent)
      if (setupError) return setupError
      await queue
      const runId = currentRun(exec.agent)
      const result = await toolBridge({ action: 'report', run_id: runId, detail: args.detail === true }, exec.signal)
      return withDiagnostics(result, runId, args.detail === true)
    },
  }))

  ctx.systemPrompt.section({
    name: 'generalrouter:host-admission', order: 89,
    text: 'GeneralRouter no longer prefilters candidates by membership in DSH native subagent model selection policy. That projection, when present, supplies optional effort metadata only; absent or empty metadata does not block matching or same-model repair preparation. Router still checks configured mappings, observed quota admission and dispatch tickets. Returned dispatch arguments do not guarantee DSH will accept the call: the host retains its runtime restrictions. If the native call is rejected, report and retain the actual failure, do not claim a worker ran, and do not automatically substitute a model or switch to a different plugin entry point. This does not enable providers, change the user\'s allowlist, or add cross-plugin dispatch.',
  })
  ctx.systemPrompt.section({
    name: 'generalrouter:dispatch', order: 90,
    text: `GeneralRouter selects a model only for the first execution of each independent development task and then freezes its provider/model/effort. The parent uses generalrouter-task-profile to prepare one plan, submits profile_json and original_request to generalrouter_match once, then reuses its profile_id for other independent tasks. When ready_for_main_agent, copy dispatch into native ${settings.delegationToolName}, preserving its reservation marker and exact route; absent effort means provider default. Add only necessary upstream results. Execution completion and usage are recorded automatically. Workers report failures and evidence to the parent. After reading the worker's evidence, record acceptance separately with generalrouter_record_acceptance: passed, failed or unknown, with scope and evidence. Check contradictions or missing evidence as needed; do not routinely rerun the entire acceptance suite. An ended task/worker is never reopened. The parent decides whether to repair. For a repair, call generalrouter_prepare_repair with the saved profile_id, the ended repairs_task_id, a new task_id, a specific objective and a concise memento of attempted strategies, failures, evidence and next steps, plus necessary input_artifacts. Dispatch its returned arguments into a fresh native subagent using the exact original frozen provider/model/effort. This preparation only admits and records the inherited route; it never generates new weights, re-ranks candidates, or substitutes a model. If that route is blocked, report the block to the parent. generalrouter_append_task and new matching are only for additional independent tasks, never a way to rename or re-route repair work. A repair link is not depends_on; a known failed dependency cannot be treated as successful. A passing repair never changes its original task's failure. Keep the original profile unchanged. Never bypass tickets, automatically loop repairs, or add a reviewer without task need. Tickets do not deduct quota. Do not routinely reread ledgers/source, poll status, or repeat matching just to test completion. After delivery read one summary report; use detail:true only for specific diagnostics. Task and repair-group totals are alternate views, not additive costs. Recording diagnostics cover observed failures, not guaranteed usage completeness.`,
  })
}
