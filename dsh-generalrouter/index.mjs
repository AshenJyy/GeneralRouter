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
export const inject = ['tools', 'systemPrompt', 'skills']
export const Config = z.object({
  // Empty paths follow the Agent Session's original project, not the Host cwd.
  projectRoot: z.string().default(''),
  hostConfig: z.string().default(''),
  runtimeRoot: z.string().default('runtime'),
  pythonExecutable: z.string().default('py'),
  pythonArguments: z.array(z.string()).default(['-3']),
  bridgeTimeoutMs: z.natural().min(1000).max(120000).default(60000),
  // Accepted for old local configs; it no longer limits worker tools.
  delegationToolName: z.string().default('subagent'),
  activeQuotaRefreshSeconds: z.natural().min(600).max(86400).default(600),
})

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

function safeError(error) {
  // Do not echo subprocess stderr, the profile, prompts, or environment values.
  return error instanceof Error ? error.message : 'GeneralRouter bridge operation failed'
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
    activeQuotaRefreshSeconds: Math.max(600, config.activeQuotaRefreshSeconds || 600),
  }
  registerBundledSkill(ctx, settings.runtimeRoot)
  const lifecycle = new AbortController()
  const quotaLifecycle = new AbortController()
  const states = new Map()
  const roots = new Set()
  const bindings = new Map()
  const diagnostics = new Map()
  const linkedChildren = new Map()
  const delegationTools = new Set(['subagent', 'subagent_codex', 'subagent_antigravity'])
  if (config.delegationToolName) delegationTools.add(config.delegationToolName)
  let queue = Promise.resolve()
  let quotaTimer
  let quotaPending

  const stateFor = session => {
    let state = states.get(session.id)
    if (!state) {
      state = { session, records: new Map(), attempts: new Map(), running: false }
      states.set(session.id, state)
    }
    return state
  }
  const rootFor = state => {
    const seen = new Set()
    let current = state
    while (current && !seen.has(current.session.id)) {
      const id = current.session.id
      seen.add(id)
      if (roots.has(id)) return id
      if (linkedChildren.has(id)) return linkedChildren.get(id)
      current = current.session.header.origin === 'subagent'
        ? states.get(current.session.header.parentSession) : undefined
    }
  }
  const bindProject = (session, runId) => {
    if (bindings.has(runId)) return
    const rootSession = states.get(runId)?.session || session
    const projectRoot = config.projectRoot?.trim() || rootSession.header.cwd || ctx.get('workingDirectory')?.get(rootSession)
    if (typeof projectRoot !== 'string' || !isAbsolute(projectRoot)) {
      throw new Error('Open the project folder in DSH, or set an absolute GeneralRouter projectRoot override.')
    }
    bindings.set(runId, {
      ...settings, projectRoot: resolve(projectRoot),
      hostConfig: resolve(projectRoot, config.hostConfig?.trim() || 'host.local.json'),
      configExplicit: Boolean(config.hostConfig?.trim()),
    })
    diagnostics.set(runId, new RecordingDiagnostics(resolve(projectRoot), runId))
  }
  const bridge = async (envelope, signal) => {
    const bound = bindings.get(envelope.run_id)
    if (!bound) throw new Error('GeneralRouter has no project directory for this record.')
    const recorder = isRecordingAction(envelope.action) ? diagnostics.get(envelope.run_id) : undefined
    try {
      const result = await callBridge(bound, envelope, signal)
      await recorder?.update(envelope, result.ok === true ? undefined : result)
      return result
    } catch (error) {
      await recorder?.update(envelope, error)
      throw error
    }
  }
  const enqueue = envelope => {
    const operation = queue.then(() => bridge(envelope, lifecycle.signal))
    // Recording failures never become a host permission decision.
    queue = operation.then(() => undefined, () => undefined)
    return operation
  }
  const writeRecord = (state, record) => {
    const runId = rootFor(state)
    if (!runId) return
    void enqueue({ action: 'record_usage', run_id: runId, record: {
      ...record, role: state.session.id === runId ? 'planner' : 'worker',
    } }).catch(() => {})
  }
  const flushState = state => {
    for (const record of state.records.values()) writeRecord(state, record)
  }
  const activeRuns = () => new Set([...states.values()]
    .filter(state => state.running).map(rootFor).filter(Boolean))
  const scheduleQuota = () => {
    if (quotaLifecycle.signal.aborted) return
    if (!activeRuns().size) {
      clearTimeout(quotaTimer)
      quotaTimer = undefined
      return
    }
    if (quotaTimer || quotaPending) return
    quotaTimer = setTimeout(() => {
      quotaTimer = undefined
      refreshQuota()
    }, settings.activeQuotaRefreshSeconds * 1000)
    quotaTimer.unref?.()
  }
  const refreshQuota = () => {
    if (quotaLifecycle.signal.aborted || quotaPending) return
    const projects = new Map()
    for (const runId of activeRuns()) {
      const bound = bindings.get(runId)
      if (bound) projects.set(JSON.stringify([bound.projectRoot, bound.hostConfig, bound.userConfigFile]), runId)
    }
    if (!projects.size) return
    quotaPending = Promise.allSettled([...projects.values()].map(runId => bridge({
      action: 'refresh_quota', run_id: runId,
    }, quotaLifecycle.signal)))
    void quotaPending.finally(() => {
      quotaPending = undefined
      scheduleQuota()
    })
  }

  ctx.on('agent/status', ({ agent, status }) => {
    stateFor(agent.session).running = status === 'running'
    scheduleQuota()
  })
  ctx.on('agent/disposed', ({ agent }) => {
    stateFor(agent.session).running = false
    scheduleQuota()
  })
  // Passive observation only: no tools/pre-execute, guard or argument mutation.
  ctx.on('session/event', (session, event) => {
    if (!session.isOwnSeq(event.seq)) return
    const state = stateFor(session)
    if (event.type === 'request/header') state.route = { ...event.data.header.config }
    if (event.type === 'llm/retry-started') {
      const key = `${event.data.turn}:${event.data.step}`
      state.attempts.set(key, (state.attempts.get(key) || 0) + 1)
    }
    if (event.type !== 'assistant/message' && event.type !== 'assistant/attempt') return
    const usage = event.data.usage || lastAssistantStreamChunk(event.data.stream || [], 'usage')?.usage
    const key = `${event.data.turn}:${event.data.step}`
    const attempt = state.attempts.get(key) || 0
    const route = state.route || session.requestHeader()?.config
    const record = {
      record_id: `dsh:${session.id}:${key}:${attempt}`, session_id: session.id,
      provider: route?.provider || null, model: route?.model || null,
      reasoning_effort: route?.reasoningEffort || null,
      tokens: {
        uncached_input: usage?.inputTokens ?? null, cached_input: usage?.cacheReadTokens ?? null,
        cache_write_input: usage?.cacheWriteTokens ?? null, output: usage?.outputTokens ?? null,
        reasoning: usage?.reasoningTokens ?? null,
      },
      source: 'dsh-session-provider-usage',
    }
    state.records.set(record.record_id, record)
    writeRecord(state, record)
  })
  ctx.on('tools/result', (exec, result) => {
    if (!exec.agent || !delegationTools.has(exec.name)) return
    const runId = rootFor(stateFor(exec.agent.session))
    if (!runId) return
    const childId = result.value?.subagentId
    if (typeof childId === 'string' && childId) {
      linkedChildren.set(childId, runId)
      const child = states.get(childId)
      if (child) flushState(child)
    }
    void enqueue({ action: 'observe_tool', run_id: runId, observation: {
      call_id: exec.callId, tool_name: exec.name,
      child_session_id: typeof childId === 'string' ? childId : null,
      is_error: result.isError === true, observed_at: new Date().toISOString(),
    } }).catch(() => {})
  })
  ctx.on('dispose', async () => {
    clearTimeout(quotaTimer)
    quotaLifecycle.abort()
    if (quotaPending) await quotaPending.catch(() => {})
    await queue
    lifecycle.abort()
  })

  const invoke = async (action, args, exec) => {
    if (!exec.agent) return { ok: false, status: 'agent_session_required' }
    const state = stateFor(exec.agent.session)
    const runId = rootFor(state) || exec.agent.session.id
    try {
      bindProject(exec.agent.session, runId)
      if (action === 'match') {
        roots.add(runId)
        state.running = exec.agent.status === 'running'
        for (const known of states.values()) if (rootFor(known) === runId) flushState(known)
      }
      if (action === 'report') await queue
      const result = await bridge({ ...args, action, run_id: runId }, exec.signal)
      if (action === 'match') refreshQuota()
      scheduleQuota()
      const diagnostic = await diagnostics.get(runId)?.summary(args.detail === true)
      if (diagnostic && (action === 'report' || action === 'status' || diagnostic.unresolved_count || diagnostic.storage_error)) {
        result.recording_diagnostics = diagnostic
      }
      return result
    } catch (error) {
      if (exec.signal?.aborted) throw error
      return { ok: false, status: 'router_operation_unavailable', message: safeError(error),
        execution_blocked_by_router: false }
    }
  }

  ctx.tools.register(defineTool({
    name: 'generalrouter_match',
    description: 'Choose model and preferred effort from task-profile.v3 and return scores and reasons. This is selection only: the main agent resolves its existing native, Codex or Antigravity tool and executes. No provider binding, dispatch arguments, ticket, allowlist check, quota gate or execution-state lock. With task_id select that task; otherwise return all tasks. detail adds candidate rankings.',
    parameters: {
      profile_json: { type: 'string', description: 'Complete task-profile.v3 JSON. A new or revised plan can be supplied at any time.' },
      profile_id: { type: 'string', description: 'Optional saved profile ID instead of repeating profile_json.' },
      original_request: { type: 'string', description: 'Original requirements and constraints accompanying profile_json.' },
      task_id: { type: 'string', description: 'Optional task ID to select from the supplied or saved profile.' },
      detail: { type: 'boolean', description: 'Include the full candidate ranking; default false.' },
    }, output: jsonOutput,
    async execute(args, exec) {
      const request = { ...args }
      delete request.profile_json
      if (args.profile_json !== undefined) {
        try { request.profile = JSON.parse(args.profile_json) }
        catch { return { ok: false, status: 'invalid_profile_json' } }
      }
      return invoke('match', request, exec)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'generalrouter_record_result',
    description: 'Optional parent-reported outcome/evidence for a saved selection. Never required before or after worker calls. This records an assessment, not a verified test result or execution permission. Linking a real child_session_id can associate already-observed usage; missing usage stays unknown.',
    parameters: {
      selection_id: { type: 'string', required: true },
      execution_status: { type: 'string', enum: ['unknown', 'running', 'completed', 'failed', 'cancelled', 'not_started'] },
      acceptance_status: { type: 'string', enum: ['unknown', 'passed', 'failed'] },
      summary: { type: 'string' }, evidence: { type: 'array', items: { type: 'string' } },
      tool_name: { type: 'string' }, provider: { type: 'string' }, model: { type: 'string' }, effort: { type: 'string' },
      child_session_id: { type: 'string', description: 'Actual observed child ID, if available; never invent it.' },
    }, output: jsonOutput,
    execute: (args, exec) => invoke('record_result', args, exec),
  }))
  ctx.tools.register(defineTool({
    name: 'generalrouter_status',
    description: 'Optional saved selection/result records and cached quota reference. Quota does not authorize or block execution.',
    parameters: { refresh: { type: 'boolean', description: 'Request quota collection subject to the ten-minute cache.' }, detail: { type: 'boolean' } },
    output: jsonOutput, execute: (args, exec) => invoke('status', args, exec),
  }))
  ctx.tools.register(defineTool({
    name: 'generalrouter_report',
    description: 'Optional report of saved recommendations, reported results and passively observed DSH usage. External plugin workers may not expose token events here. Missing usage/identity remains unknown. Costs are observed API-equivalent subtotals, not guaranteed complete workflow costs or subscription bills.',
    parameters: { detail: { type: 'boolean', description: 'Include individual records; default false.' } },
    output: jsonOutput, execute: (args, exec) => invoke('report', args, exec),
  }))
  ctx.systemPrompt.section({
    name: 'generalrouter:selection', order: 90,
    text: 'GeneralRouter selects models only. When routing is requested, use generalrouter-task-profile to estimate task weights and generalrouter_match for model/effort recommendations. Resolve the selected model through your existing tools, including native subagent, subagent_codex or subagent_antigravity as appropriate; Router does not bind it to ECPA. Set the corresponding effort if the chosen tool supports it, otherwise use default and disclose that fallback. You own execution and failures. For a repair, retain the model and start a new subagent with a concise memento; no Router rematch or repair-preparation call is needed. Recording/report tools are optional and never execution prerequisites. A selection is not evidence of actual execution or successful acceptance. Unknown plugin usage stays unknown.',
  })
}
