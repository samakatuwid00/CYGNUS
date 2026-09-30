// Cygnus desktop shell: a tray icon, Ctrl+Alt+J, and one window that switches
// between a sticky panel, the full HUD and a mini orb (modelled on Sticky
// Brain). The backend stays in the repo: this process starts, watches and
// stops it, or attaches to one already running (ELECTRON_PLAN.md, phase 2
// and "Development workflow").
const { app, BrowserWindow, Tray, Menu, desktopCapturer, globalShortcut, ipcMain, nativeImage, powerMonitor, screen, session, shell } = require('electron')
const { spawn, execFileSync } = require('child_process')
const fs = require('fs')
const http = require('http')
const net = require('net')
const path = require('path')

// A child's exit handler can still write to a console that has gone away and
// throw EPIPE, which kills the main process with an error dialog (Sticky Brain
// hit exactly this). Must be in place before anything is spawned.
for (const stream of [process.stdout, process.stderr]) {
  stream.on('error', err => { if (err && err.code === 'EPIPE') return; throw err })
}

// Installed (npm run dist), the backend and its own Python sit read-only in
// resources/, and the settings and everything Cygnus writes go to the user's
// folder, %APPDATA%/Cygnus. From the repository, all of it is the repository.
const PACKAGED = app.isPackaged
const REPO = PACKAGED ? path.join(process.resourcesPath, 'backend') : path.resolve(__dirname, '..')
const PYTHON = PACKAGED ? path.join(process.resourcesPath, 'python', 'python.exe')
  : path.join(REPO, '.venv', 'Scripts', 'python.exe')
const ENV_FILE = PACKAGED ? path.join(app.getPath('userData'), '.env') : path.join(REPO, '.env')
if (PACKAGED && !fs.existsSync(ENV_FILE)) {
  // First run: the example settings, with no key in them yet - the setup
  // screen asks for it (cygnus/setup, web/setup.html).
  fs.mkdirSync(path.dirname(ENV_FILE), { recursive: true })
  fs.copyFileSync(path.join(REPO, '.env.example'), ENV_FILE)
}
const PORT = Number(readEnv('JARVIS_PORT') || 8000)
const HUD_URL = `http://127.0.0.1:${PORT}/`
const HUD_ORIGIN = HUD_URL.slice(0, -1)
const HOTKEY = 'Control+Alt+J'
// A ring on a transparent background: the app icon shrunk to 16px was a dark
// square that vanished on a dark taskbar. tray@2x.png is picked on scaled
// displays.
const TRAY_ICON = path.join(__dirname, 'assets', 'tray.png')
// The backend's runtime data lives in data/ (or CYGNUS_DATA_DIR), like cygnus/paths.py.
const DATA_DIR = PACKAGED ? path.join(app.getPath('userData'), 'data')
  : (readEnv('CYGNUS_DATA_DIR') || path.join(REPO, 'data'))
const LOG_FILE = path.join(DATA_DIR, 'logs', 'desktop-backend.log')
const READY_TIMEOUT_MS = 180000
// What the tray checks besides the backend: Chrome started with its debugging
// port (clicks inside the browser) and Ollama (the offline fallback), read
// from the same settings the backend uses.
const CHROME_PORT = Number(readEnv('JARVIS_BROWSER_PORT') || 9223)
const OLLAMA_PORT = Number(new URL(readEnv('OLLAMA_BASE_URL') || 'http://localhost:11434/v1').port || 11434)
// 9router, the brain's primary backend. Nothing else on the machine starts it
// since the OmniRoute app went away, and a router that is down drops every
// turn to the local Ollama model without saying so.
const ROUTER_PORT = Number(new URL(readEnv('ROUTER_BASE_URL') || 'http://localhost:20128/v1').port || 20128)
const ROUTER_CMD = path.join(process.env.APPDATA || '', 'npm', '9router.cmd')
// Only the author's setup uses 9router: no provider named (the settings from
// before providers), or CYGNUS_PROVIDER=9router. A student's Gemini or Groq
// key needs no router, and a tray warning about one would only confuse.
const PROVIDER = readEnv('CYGNUS_PROVIDER').toLowerCase()
const ROUTER_WANTED = !PROVIDER || PROVIDER === '9router'
const OLLAMA_WANTED = PROVIDER === 'ollama' || /^true$/i.test(readEnv('JARVIS_USE_OLLAMA') || (PROVIDER ? 'false' : 'true'))
const ROUTER_READY_MS = 45000
const HEALTH_EVERY_MS = 10000
const CHECKS_EVERY_MS = 60000
const MAX_RESTARTS = 3
const RESTART_WINDOW_MS = 10 * 60 * 1000
// The backend's "start me again" after the setup saved a provider
// (cygnus/server/setup_routes.py RESTART_REQUESTED).
const RESTART_REQUESTED = 75
// 560 tall since the transcript is trimmed to the last exchange (owner's
// choice, CYGNUS_UI_PLAN.md step 8).
const STICKY = { width: 380, height: 560, margin: 20, minWidth: 320, minHeight: 420 }
const MINI = { size: 96, margin: 24 }
const MODES = ['sticky', 'expanded', 'mini']

const startHidden = process.argv.includes('--hidden')
const isDev = process.argv.includes('--dev')
// A dev run gets its own profile and single-instance lock, so `npm run dev`
// next to an installed copy starts instead of only focusing that copy.
if (isDev) app.setPath('userData', app.getPath('userData') + ' (dev)')

let win = null
let tray = null
let mode = 'sticky'
let fullScreen = false     // the full HUD over the whole display, taskbar too
let stickyBounds = null
let dragOrigin = null
let hotkeyOk = false
let quitting = false
// state: starting | ready | stopped | error. attached: the backend is someone
// else's (a dev server started by hand), so it is never restarted or killed.
let backend = { proc: null, attached: false, state: 'starting', note: '' }
let checks = null          // tray warnings; null until the first check ran
let restartTimes = []      // when an owned backend was restarted after dying

// ---------------------------------------------------------------- backend

// A setting from the environment, else from the repo's .env (as the backend
// reads it), else ''.
function readEnv (name) {
  if (process.env[name]) return process.env[name]
  try {
    const m = fs.readFileSync(ENV_FILE, 'utf8').match(new RegExp(`^\\s*${name}\\s*=\\s*(.+?)\\s*$`, 'm'))
    if (m) return m[1].replace(/^["']|["']$/g, '')
  } catch {}
  return ''
}

// What answers on the port: 'jarvis' (the HUD page), 'other', or 'free'.
// The HUD's own shell element, not its title: the title is branding. It sits
// past the page's long <style>: keeping only the first chunk (64 KB) missed
// it once the Running panel's styles pushed it to char 66,837, and the window
// showed "did not answer within 3 minutes" over a backend that was up
// (2026-09-14). Read until the marker turns up, at most 1 MB.
const HUD_MARK = 'id="jv-shell"'
function probe () {
  return new Promise(resolve => {
    const req = http.get(HUD_URL, { timeout: 1500 }, res => {
      let body = ''
      let found = false
      res.on('data', chunk => {
        if (found || body.length > 1048576) return
        body += chunk
        found = body.includes(HUD_MARK)
      })
      res.on('end', () => resolve(res.statusCode === 200 && found ? 'jarvis' : 'other'))
    })
    req.on('timeout', () => req.destroy())
    req.on('error', () => resolve('free'))
  })
}

const pidFile = () => path.join(app.getPath('userData'), 'backend.pid')

function isPython (pid) {
  try {
    return /python/i.test(execFileSync('tasklist', ['/FI', `PID eq ${pid}`, '/FO', 'CSV', '/NH'], { encoding: 'utf8' }))
  } catch { return false }
}

function killTree (pid) {
  try { execFileSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' }) } catch {}
}

// A backend left behind by a crashed shell still holds the port and the mic.
// Only a python process is killed, in case Windows reused the pid.
function killStaleBackend () {
  let pid = 0
  try { pid = Number(fs.readFileSync(pidFile(), 'utf8')) } catch { return }
  try { fs.unlinkSync(pidFile()) } catch {}
  if (pid && isPython(pid)) killTree(pid)
}

function setBackend (state, note = '') {
  backend.state = state
  backend.note = note
  console.log(`[desktop] backend ${state}${backend.attached ? ' (attached)' : ''}${note ? ': ' + note : ''}`)
  buildTrayMenu()
  if (state === 'error') showStatusPage(note)
}

async function startBackend () {
  killStaleBackend()
  const found = await probe()
  if (found === 'jarvis') {
    backend.attached = true
    return onBackendReady()
  }
  if (found === 'other') return setBackend('error', `Port ${PORT} is used by another program.`)
  if (!fs.existsSync(PYTHON)) return setBackend('error', 'No .venv in the repo (ELECTRON_PLAN.md, phase 0).')
  const proc = spawnBackend()
  await waitForReady(proc)
}

function spawnBackend () {
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true })
  const log = fs.openSync(LOG_FILE, 'a')
  const proc = spawn(PYTHON, ['-m', 'cygnus'], {
    cwd: REPO,
    windowsHide: true,
    stdio: ['ignore', log, log],
    env: {
      ...process.env, JARVIS_PORT: String(PORT), JARVIS_HOST: '127.0.0.1', PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8', CYGNUS_DESKTOP: '1', CYGNUS_ENV_FILE: ENV_FILE, CYGNUS_DATA_DIR: DATA_DIR
    }
  })
  fs.writeFileSync(pidFile(), String(proc.pid))
  backend = { proc, attached: false, state: 'starting', note: '' }
  proc.on('exit', code => {
    try { fs.closeSync(log) } catch {}
    if (backend.proc !== proc) return
    try { fs.unlinkSync(pidFile()) } catch {}
    backend.proc = null
    onBackendExit(code)
  })
  buildTrayMenu()
  return proc
}

// An owned backend that dies on its own is started again, but at most three
// times in ten minutes: one that keeps crashing needs a person, not a loop.
// A deliberate stop never gets here (stopBackend lets go of the process first).
function onBackendExit (code) {
  // The setup saved a new provider and asked to start again with it
  // (cygnus/server/setup_routes.py): not a crash, and no budget spent.
  if (code === RESTART_REQUESTED) {
    setBackend('starting', 'Applying the new settings.')
    showStatusPage('Applying your settings…')
    return startBackend()
  }
  const now = Date.now()
  restartTimes = restartTimes.filter(t => now - t < RESTART_WINDOW_MS)
  if (restartTimes.length >= MAX_RESTARTS) {
    return setBackend('stopped', `The backend keeps exiting (code ${code}). See the backend log.`)
  }
  restartTimes.push(now)
  setBackend('starting', `The backend exited (code ${code}); starting it again.`)
  showStatusPage('Cygnus stopped unexpectedly. Starting it again…')
  setTimeout(startBackend, 2000)
}

// "Start backend" in the tray: a fresh start, with the crash budget reset.
function startFresh () {
  restartTimes = []
  setBackend('starting')
  showStatusPage('Starting Cygnus…')
  startBackend()
}

// Whisper loads before the port opens, so "the page answers" means ready.
async function waitForReady (proc) {
  const deadline = Date.now() + READY_TIMEOUT_MS
  while (Date.now() < deadline && backend.proc === proc) {
    if (await probe() === 'jarvis') return onBackendReady()
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  if (backend.proc === proc) setBackend('error', 'The backend did not answer within 3 minutes. See the backend log.')
}

// A brain that cannot work yet (first run: no key) opens the setup, full
// size, instead of a HUD that could only say so.
async function onBackendReady () {
  setBackend('ready')
  const status = await fetchStatus()
  if (win && !win.isDestroyed()) {
    if (status && status.setup_problem) {
      showMode('expanded')
      win.loadURL(HUD_URL + 'setup')
    } else {
      win.loadURL(HUD_URL)
    }
  }
  runChecks()
}

function stopBackend () {
  const { proc } = backend
  backend.proc = null
  if (proc && proc.exitCode === null) killTree(proc.pid)
  try { fs.unlinkSync(pidFile()) } catch {}
}

async function restartBackend () {
  if (backend.attached) return
  stopBackend()
  setBackend('starting')
  showStatusPage('Restarting Cygnus…')
  await new Promise(resolve => setTimeout(resolve, 1000))
  await startBackend()
}

// ---------------------------------------------------------------- 9router

// '' until the first attempt: attached | ready | missing | failed.
let router = ''

// The router is shared with everything else that answers on this machine
// (Hermes, the coding CLIs), so one already up is left alone and one started
// here is detached: quitting Cygnus must not take the others' brain with it.
// Measured ~12s from a cold start to the port answering (2026-09-16).
async function startRouter () {
  if (!ROUTER_WANTED) return
  if (await portOpen(ROUTER_PORT)) return setRouter('attached')
  if (!fs.existsSync(ROUTER_CMD)) return setRouter('missing', 'npm i -g 9router')
  try {
    // cmd.exe by hand rather than shell:true — Node refuses to spawn a .cmd
    // without a shell, and an argument list keeps the path out of a command
    // line that would be parsed again.
    spawn(process.env.COMSPEC || 'cmd.exe',
      ['/c', ROUTER_CMD, '--tray', '--no-browser', '--skip-update', '--port', String(ROUTER_PORT)],
      { cwd: app.getPath('home'), detached: true, windowsHide: true, stdio: 'ignore' }).unref()
  } catch (err) {
    return setRouter('failed', err.message)
  }
  const deadline = Date.now() + ROUTER_READY_MS
  while (Date.now() < deadline) {
    // A shut port is refused at once, so the wait has to be its own: without
    // it this is a busy loop for the whole startup.
    await new Promise(resolve => setTimeout(resolve, 1000))
    if (await portOpen(ROUTER_PORT)) return setRouter('ready')
  }
  setRouter('failed', `port ${ROUTER_PORT} stayed shut`)
}

function setRouter (state, note = '') {
  router = state
  console.log(`[desktop] 9router ${state}${note ? ': ' + note : ''}`)
  runChecks()
}

// ---------------------------------------------------------------- health

function fetchStatus () {
  return new Promise(resolve => {
    const req = http.get(HUD_URL + 'status', { timeout: 3000 }, res => {
      let body = ''
      res.on('data', chunk => { body += chunk })
      res.on('end', () => { try { resolve(JSON.parse(body)) } catch { resolve(null) } })
    })
    req.on('timeout', () => req.destroy())
    req.on('error', () => resolve(null))
  })
}

// An attached dev server that goes away shows in the tray, and one that
// answers again is picked up. An owned backend reports its own exit instead.
async function healthTick () {
  if (backend.state === 'starting' || backend.proc) return
  const alive = await probe() === 'jarvis'
  if (alive && backend.state !== 'ready') {
    backend.attached = true
    onBackendReady()
  } else if (!alive && backend.state === 'ready') {
    setBackend('stopped', 'Your dev server stopped.')
  }
}

function portOpen (port) {
  return new Promise(resolve => {
    const socket = net.connect({ host: '127.0.0.1', port, timeout: 1000 })
    socket.once('connect', () => { socket.destroy(); resolve(true) })
    socket.once('timeout', () => { socket.destroy(); resolve(false) })
    socket.once('error', () => resolve(false))
  })
}

// Windows can switch the microphone off for all desktop apps, and the HUD
// then only shows a dead mic. "Deny" at the machine, user or desktop-app
// level blocks it.
const MIC_STORE = 'Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone'
function micBlocked () {
  const keys = [`HKLM\\SOFTWARE\\${MIC_STORE}`, `HKCU\\Software\\${MIC_STORE}`, `HKCU\\Software\\${MIC_STORE}\\NonPackaged`]
  return keys.some(key => {
    try { return /\bDeny\b/.test(execFileSync('reg', ['query', key, '/v', 'Value'], { encoding: 'utf8' })) } catch { return false }
  })
}

async function runChecks () {
  const found = []
  if (micBlocked()) found.push('Microphone blocked for desktop apps (Settings > Privacy > Microphone)')
  if (ROUTER_WANTED && !await portOpen(ROUTER_PORT)) {
    found.push(router === 'missing'
      ? '9router is not installed: every turn falls back to the local model'
      : '9router is not running: every turn falls back to the local model')
  }
  if (OLLAMA_WANTED && !await portOpen(OLLAMA_PORT)) found.push('Ollama is not running: no offline fallback')
  if (!await portOpen(CHROME_PORT)) found.push('The automation Chrome is not open: no clicks inside the browser')
  const wake = backend.state === 'ready' ? ((await fetchStatus()) || {}).wake_server : null
  if (wake && !wake.healthy) found.push(`Wake word off: ${wake.error || 'microphone unavailable'}`)
  if (checks === null || found.join('|') !== checks.join('|')) {
    console.log(`[desktop] checks: ${found.join(' | ') || 'all clear'}`)
  }
  checks = found
  buildTrayMenu()
}

// ---------------------------------------------------------------- window

function showStatusPage (text) {
  if (win && !win.isDestroyed()) win.loadFile(path.join(__dirname, 'starting.html'), { query: { msg: text } })
}

function createWindow () {
  const { workArea } = screen.getPrimaryDisplay()
  stickyBounds = {
    width: STICKY.width,
    height: Math.min(STICKY.height, workArea.height - 2 * STICKY.margin),
    x: workArea.x + workArea.width - STICKY.width - STICKY.margin,
    y: workArea.y + STICKY.margin
  }
  win = new BrowserWindow({
    ...stickyBounds,
    minWidth: STICKY.minWidth,
    minHeight: STICKY.minHeight,
    title: 'Cygnus',
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
      autoplayPolicy: 'no-user-gesture-required'
    }
  })
  win.setAlwaysOnTop(true, 'floating')
  guardWindow()
  // Escape leaves full screen, as it does in any app. Not prevented: the
  // page still gets the key.
  win.webContents.on('before-input-event', (_e, input) => {
    if (fullScreen && input.type === 'keyDown' && input.key === 'Escape') setFullScreenView(false)
  })
  // A reload rebuilds the page laid out for the default mode; tell it again.
  win.webContents.on('did-finish-load', () => win.webContents.send('desk:mode', mode))
  // Closing (Alt+F4) shrinks to the orb, as Sticky Brain's close shrinks to its
  // pet; quitting is a tray decision.
  win.on('close', event => { if (!quitting) { event.preventDefault(); setMode('mini') } })
  win.once('ready-to-show', () => { if (!startHidden) win.show() })
  showStatusPage('Starting Cygnus…')
}

// The window only ever shows JARVIS. Other pages open in the default browser,
// and the one permission granted is the microphone, to the HUD's own origin.
function guardWindow () {
  const openOutside = url => { if (/^https?:/i.test(url)) shell.openExternal(url) }
  win.webContents.setWindowOpenHandler(({ url }) => { openOutside(url); return { action: 'deny' } })
  win.webContents.on('will-navigate', (event, url) => {
    if (url.startsWith(HUD_URL)) return
    event.preventDefault()
    openOutside(url)
  })
  session.defaultSession.setPermissionRequestHandler((_wc, permission, callback, details) =>
    callback(permission === 'media' && (details.requestingUrl || '').startsWith(HUD_URL)))
  session.defaultSession.setPermissionCheckHandler((_wc, permission, origin) =>
    permission === 'media' && origin === HUD_ORIGIN)
}

// The page is told first, then the window resized, so the frame the resize
// lands on already has the new layout. Minimum size is lowered before the
// bounds and resizability dropped after them: Windows clamps silently
// otherwise (both learned in Sticky Brain).
function setMode (next) {
  if (!win || win.isDestroyed() || !MODES.includes(next)) return
  if (mode === 'sticky') stickyBounds = win.getBounds()
  mode = next
  fullScreen = false
  win.webContents.send('desk:mode', mode)
  const { workArea } = screen.getDisplayMatching(stickyBounds)
  if (mode === 'mini') {
    // Locked both ways: at 125% scaling Windows let the orb grow while dragged.
    win.setMinimumSize(MINI.size, MINI.size)
    win.setMaximumSize(MINI.size, MINI.size)
    win.setBounds({
      x: workArea.x + workArea.width - MINI.size - MINI.margin,
      y: workArea.y + workArea.height - MINI.size - MINI.margin,
      width: MINI.size,
      height: MINI.size
    })
    win.setResizable(false)
  } else {
    win.setResizable(true)
    win.setMaximumSize(0, 0)                      // 0 = no maximum
    win.setMinimumSize(STICKY.minWidth, STICKY.minHeight)
    win.setBounds(mode === 'expanded' ? workArea : stickyBounds)
  }
  // The full HUD is a normal window; the panel and the orb float.
  win.setAlwaysOnTop(mode !== 'expanded', 'floating')
  // Only the full HUD gets a taskbar entry, so it can be minimized there
  // like any other window instead of only living in the tray.
  win.setSkipTaskbar(mode !== 'expanded')
  buildTrayMenu()
}

function showMode (next, focus = true) {
  setMode(next)
  if (win.isMinimized()) win.restore()        // a minimized full HUD, asked back

  if (focus) { win.show(); win.focus() } else if (!win.isVisible()) win.showInactive()
}

function onHotkey () {
  if (!win || win.isDestroyed()) return
  showMode(mode === 'mini' ? 'sticky' : mode)
  win.webContents.send('desk:hotkey')
}

ipcMain.on('desk:setMode', (_e, next) => showMode(next, next !== 'mini'))

// "Full screen yourself": the full HUD over the whole display, taskbar
// included. The display's bounds, not setFullScreen: a frameless transparent
// window is not reliably fullscreened by Windows, and every other view is
// placed with setBounds already. 'screen-saver' is the level that sits above
// the taskbar. Escape, or any view asked for, leaves it.
function setFullScreenView (on) {
  if (!win || win.isDestroyed()) return
  if (mode !== 'expanded') setMode('expanded')
  fullScreen = !!on
  const display = screen.getDisplayMatching(win.getBounds())
  win.setBounds(fullScreen ? display.bounds : display.workArea)
  win.setAlwaysOnTop(fullScreen, 'screen-saver')
  if (win.isMinimized()) win.restore()
  win.show(); win.focus()
  buildTrayMenu()
}

ipcMain.on('desk:fullScreen', (_e, on) => setFullScreenView(on !== false))

// Native minimize in expanded mode, where the window has a taskbar entry to
// fall back to (see setSkipTaskbar in setMode). The panel and the orb have
// none, so "minimize yourself" said there hides to the tray instead; the
// wake word, the hotkey and the tray bring it back (desk:surface).
ipcMain.on('desk:minimize', () => {
  if (!win || win.isDestroyed()) return
  if (mode === 'expanded') win.minimize()
  else win.hide()
})

// A wake word heard while JARVIS is an orb or hidden brings the panel back
// without taking focus from whatever the owner is typing in.
ipcMain.on('desk:surface', () => {
  if (!win || win.isDestroyed()) return
  if (mode === 'mini') setMode('sticky')
  if (win.isMinimized()) win.restore()
  if (!win.isVisible()) win.showInactive()
})

// The orb and the panel's top bar are dragged by hand, not with a drag
// region: a drag region swallows the clicks on and under it (Sticky Brain's
// pet works the same way). The full HUD fills the screen and never moves.
// setBounds with the size taken at the start, never setPosition: at 125%
// scaling each setPosition rounded the size up a pixel, and the orb grew.
ipcMain.on('desk:dragStart', () => {
  dragOrigin = win && !win.isDestroyed() && mode !== 'expanded' ? win.getBounds() : null
})
ipcMain.on('desk:dragMove', (_e, d) => {
  if (!dragOrigin || mode === 'expanded') return
  win.setBounds({
    x: Math.round(dragOrigin.x + (Number(d && d.dx) || 0)),
    y: Math.round(dragOrigin.y + (Number(d && d.dy) || 0)),
    width: dragOrigin.width,
    height: dragOrigin.height
  })
})
ipcMain.on('desk:dragEnd', () => {
  if (dragOrigin && mode === 'sticky') stickyBounds = win.getBounds()
  dragOrigin = null
})

// Live thumbnails of open windows for the HUD's Running Now rows. The page is
// sandboxed and cannot capture windows itself; a source id is
// "window:<handle>:0", which the rows carry. Only when a row is opened.
ipcMain.handle('desk:thumbs', async () => {
  const sources = await desktopCapturer.getSources({ types: ['window'], thumbnailSize: { width: 360, height: 225 } })
  return sources.map(s => ({ id: s.id, name: s.name, img: s.thumbnail.isEmpty() ? '' : s.thumbnail.toDataURL() }))
})

// ---------------------------------------------------------------- tray

function trayStatus () {
  if (backend.state === 'ready') return backend.attached ? 'Attached to your dev server' : 'Running'
  if (backend.state === 'starting') return 'Starting…'
  return backend.note || 'Backend stopped'
}

function buildTrayMenu () {
  if (!tray) return
  const status = trayStatus()
  const warnings = checks || []
  const ready = backend.state === 'ready'
  tray.setToolTip(`Cygnus (${status})${warnings.length ? `, ${warnings.length} warning(s)` : ''}${isDev ? ' [dev]' : ''}`)
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: status, enabled: false },
    ...warnings.map(w => ({ label: `⚠ ${w}`, enabled: false })),
    ...(hotkeyOk ? [] : [{ label: 'Ctrl+Alt+J is taken by another app', enabled: false }]),
    { type: 'separator' },
    { label: 'Panel', type: 'radio', checked: mode === 'sticky', click: () => showMode('sticky') },
    { label: 'Full HUD', type: 'radio', checked: mode === 'expanded' && !fullScreen, click: () => showMode('expanded') },
    { label: 'Full screen', type: 'radio', checked: fullScreen, click: () => setFullScreenView(true) },
    { label: 'Mini orb', type: 'radio', checked: mode === 'mini', click: () => showMode('mini', false) },
    { label: 'Hide', click: () => win.hide() },
    { type: 'separator' },
    {
      label: ready ? 'Restart backend' : 'Start backend',
      enabled: backend.state !== 'starting' && !(ready && backend.attached),
      click: ready ? restartBackend : startFresh
    },
    { label: 'Open backend log', click: () => shell.openPath(LOG_FILE) },
    { type: 'separator' },
    { label: 'Quit Cygnus', click: () => { quitting = true; app.quit() } }
  ]))
}

// An empty image is an invisible tray entry: started hidden, that would leave
// no way into the app at all (Sticky Brain's first build had this).
function createTray () {
  const image = nativeImage.createFromPath(TRAY_ICON)
  if (image.isEmpty()) console.error(`[desktop] tray icon missing: ${TRAY_ICON}`)
  tray = new Tray(image)
  tray.on('click', () => showMode(mode === 'mini' ? 'sticky' : mode))
  buildTrayMenu()
  showTrayHintOnce()
}

// Windows 11 puts new tray icons under the ^ arrow, so on first start JARVIS
// says where it went. Once per profile, behind a marker file (the same
// pattern Sticky Brain uses for autostart).
function showTrayHintOnce () {
  const marker = path.join(app.getPath('userData'), 'tray-hint-shown')
  if (fs.existsSync(marker)) return
  tray.displayBalloon({
    title: 'Cygnus is running',
    content: 'Ctrl+Alt+J opens the panel. The tray icon may be under the ^ arrow on the taskbar; drag it onto the taskbar to keep it visible.'
  })
  try {
    fs.mkdirSync(path.dirname(marker), { recursive: true })
    fs.writeFileSync(marker, new Date().toISOString(), 'utf8')
  } catch {}
}

// ---------------------------------------------------------------- app

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { if (win) showMode(mode === 'mini' ? 'sticky' : mode) })
  app.whenReady().then(() => {
    createWindow()
    createTray()
    hotkeyOk = globalShortcut.register(HOTKEY, onHotkey)
    if (!hotkeyOk) console.error(`[desktop] ${HOTKEY} could not be registered`)
    startRouter()
    startBackend()
    setInterval(healthTick, HEALTH_EVERY_MS)
    setInterval(runChecks, CHECKS_EVERY_MS)
    // After sleep the page's microphone stream is dead; a reload gives it a
    // new one (the backend's wake engine reopens its own). Checks rerun too.
    powerMonitor.on('resume', () => setTimeout(() => {
      if (backend.state === 'ready' && win && !win.isDestroyed()) win.loadURL(HUD_URL)
      runChecks()
    }, 5000))
  })
  app.on('before-quit', () => { quitting = true })
  app.on('will-quit', () => {
    globalShortcut.unregisterAll()
    if (!backend.attached) stopBackend()
  })
}
