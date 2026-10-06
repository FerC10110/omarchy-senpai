import QtQuick
import Quickshell
import Quickshell.Io
import "SenpaiModel.js" as Model

// Service.qml -- the shared state of io.github.ferc10110.senpai: what plays,
// what downloads, the settings, and every call into bin/senpai. The bar
// widget and the overlay read it through shell.serviceFor / the injected
// `service`. keepLoaded, so it survives plugin hot reloads.
Item {
  id: root

  property var shell: null
  property var manifest: null
  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : Model.PLUGIN_ID

  readonly property string pluginDir: decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "").replace(/\/$/, ""))
  readonly property string helperPath: pluginDir + "/bin/senpai"
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR")
      ? Quickshell.env("XDG_RUNTIME_DIR") + "/senpai"
      : Quickshell.env("HOME") + "/.cache/senpai/run"

  // ---- state the UI reads
  property var playing: null            // now-playing.json, or null
  property var subtitle: null           // from the latest "playing" event
  property bool paused: false
  property real position: 0
  property real duration: 0
  property var downloads: []
  property var home: ({ continue: null, recent: [] })
  property bool busy: false
  property string lastError: ""
  readonly property bool isPlaying: playing !== null
  readonly property int activeDownloads: Model.countRunning(downloads)

  // ---- settings: the bar entry in shell.json, defaults applied. Our own
  // write applies at once and stays authoritative until the host echoes it
  // back. While updateEntryInline runs, the host re-publishes barConfig
  // dozens of times with the *previous* config; trusting those echoes showed
  // the old value for ~250 ms and made a second Alt+key inside that window
  // cycle from the stale value, so the write was a no-op. A differing echo
  // wins only after 3 s (an edit made elsewhere), and only when the bar has
  // an entry for us — without one the setting has nowhere to persist.
  readonly property var hostEntry: Model.findBarEntry(shell ? shell.barConfig : null, pluginId)
  readonly property var hostSettings: Model.settingsFrom(hostEntry)
  readonly property bool hostEntryFound: Object.keys(hostEntry).length > 0
  property var ownSettings: null
  property double ownSince: 0
  property var settings: Model.settingsFrom(null)
  onHostSettingsChanged: {
    if (ownSettings !== null
        && (Model.settingsEqual(hostSettings, ownSettings) || (hostEntryFound && Date.now() - ownSince > 3000)))
      ownSettings = null
    refreshSettings()
  }
  onOwnSettingsChanged: refreshSettings()

  // One `settings` object per real change: every host echo builds a new
  // hostSettings object, and the overlay rebuilds its chips on each one.
  function refreshSettings() {
    var next = ownSettings !== null ? ownSettings : hostSettings
    if (!Model.settingsEqual(next, settings)) settings = next
  }

  function setSetting(key, value) {
    var next = {}
    var keys = Object.keys(Model.DEFAULTS)
    for (var i = 0; i < keys.length; i++) next[keys[i]] = root.settings[keys[i]]
    next[key] = value
    root.ownSince = Date.now()
    root.ownSettings = Model.settingsFrom(next)
    writeSoon.restart()
  }

  function writeOwnSettings() {
    if (root.ownSettings !== null && root.shell && typeof root.shell.updateEntryInline === "function")
      root.shell.updateEntryInline(root.pluginId, Model.entryFrom(root.ownSettings, root.pluginId))
  }

  // Every shell.json write stalls the shell for ~200 ms (it re-registers all
  // plugin widgets), so a run of Alt+key presses is written once, after it.
  Timer {
    id: writeSoon
    interval: 400
    onTriggered: { root.ownSince = Date.now(); root.writeOwnSettings(); reassert.restart() }
  }

  // Quick successive writes can leave the host's in-memory config behind the
  // file (its watcher misses an atomic rename); writing once more makes it
  // see a change and echo the real state back.
  Timer {
    id: reassert
    interval: 1000
    onTriggered: if (root.ownSettings !== null && root.hostEntryFound && !Model.settingsEqual(root.hostSettings, root.ownSettings)) root.writeOwnSettings()
  }

  function helperArgs(extra) {
    var args = root.settings.aniPyPath ? ["--ani-py", root.settings.aniPyPath] : []
    return args.concat(extra)
  }

  // ---- helper calls (one SenpaiCommand per purpose, so a search never
  // waits behind a status poll)
  function parseOut(out) {
    try { return JSON.parse(out) } catch (e) { return null }
  }

  function reportError(payload, err) {
    var message = payload && payload.error ? String(payload.error) : (String(err || "").trim().split("\n").pop() || "senpai failed")
    root.lastError = message
    console.warn("senpai:", message)
  }

  property var _searchCb: null
  function search(query, cb) {
    root._searchCb = cb
    root.busy = true
    root.lastError = ""
    searchCmd.start(helperArgs(["search", query, "--provider", root.settings.provider]))
  }
  SenpaiCommand {
    id: searchCmd
    program: root.helperPath
    onFinished: function(code, out, err) {
      root.busy = false
      var payload = root.parseOut(out)
      if (code !== 0 || !payload || payload.error) { root.reportError(payload, err); payload = null }
      var cb = root._searchCb; root._searchCb = null
      if (cb) cb(payload)
    }
  }

  property var _episodesCb: null
  function episodes(provider, id, title, cb) {
    root._episodesCb = cb
    root.busy = true
    root.lastError = ""
    episodesCmd.start(helperArgs(["episodes", provider, id, "--title", title]))
  }
  SenpaiCommand {
    id: episodesCmd
    program: root.helperPath
    onFinished: function(code, out, err) {
      root.busy = false
      var payload = root.parseOut(out)
      if (code !== 0 || !payload || payload.error) { root.reportError(payload, err); payload = null }
      var cb = root._episodesCb; root._episodesCb = null
      if (cb) cb(payload)
    }
  }

  property var _homeCb: null
  function refreshHome(cb) {
    root._homeCb = cb
    homeCmd.start(helperArgs(["home"]))
  }
  SenpaiCommand {
    id: homeCmd
    program: root.helperPath
    timeoutMs: 15000
    onFinished: function(code, out, err) {
      var payload = root.parseOut(out)
      if (code === 0 && payload && !payload.error) root.home = payload
      else root.reportError(payload, err)
      var cb = root._homeCb; root._homeCb = null
      if (cb) cb(root.home)
    }
  }

  // play / download / stop / continue share one runner: they are user
  // actions, one at a time.
  // Set by a play request; the events file may then carry the reason it failed.
  property bool playRequested: false

  function play(provider, id, title, episode) {
    root.lastError = ""
    root.playRequested = true
    actionCmd.start(helperArgs(["play", provider, id, "-e", String(episode), "--title", title].concat(Model.playArgs(root.settings))))
  }
  function download(provider, id, title, spec) {
    root.lastError = ""
    actionCmd.start(helperArgs(["download", provider, id, "-e", String(spec), "--title", title].concat(Model.playArgs(root.settings))))
  }
  function continueWatching() {
    root.lastError = ""
    root.playRequested = true
    actionCmd.start(helperArgs(["continue"].concat(Model.playArgs(root.settings))))
  }
  function stop() {
    actionCmd.start(helperArgs(["stop"]))
  }
  function next() {
    if (!root.playing) return
    root.play(root.playing.provider, root.playing.id, root.playing.title, Model.nextEpisode(root.playing.episode))
  }
  SenpaiCommand {
    id: actionCmd
    program: root.helperPath
    timeoutMs: 20000
    policy: "queue"
    onFinished: function(code, out, err) {
      var payload = root.parseOut(out)
      if (code !== 0 || !payload || payload.error) root.reportError(payload, err)
      root.refreshStatus()
      root.refreshHome(null)
    }
  }

  function refreshStatus() {
    if (!statusCmd.pending) statusCmd.start(helperArgs(["status"]))
  }
  SenpaiCommand {
    id: statusCmd
    program: root.helperPath
    timeoutMs: 10000
    onFinished: function(code, out, err) {
      var payload = root.parseOut(out)
      if (code !== 0 || !payload || payload.error) return
      root.downloads = Array.isArray(payload.downloads) ? payload.downloads : []
      root.applyPlaying(payload.playing || null)
    }
  }

  // ---- the playback itself: now-playing.json says what, the events file
  // says which subtitle, the mpv socket says where it is.
  function applyPlaying(next) {
    var was = root.playing ? String(root.playing.pid) : ""
    var now = next ? String(next.pid) : ""
    root.playing = next
    if (now === "") {
      root.subtitle = null
      root.paused = false
      root.position = 0
      root.duration = 0
      root.releaseSocket()
      return
    }
    if (now !== was) {
      root.paused = false
      root.position = 0
      root.duration = 0
      eventsFile.reload()
      root.armSocket()
    }
  }

  function togglePause() {
    if (!root.playing) return
    root.socketWrite(["set_property", "pause", !root.paused])
  }

  FileView {
    id: eventsFile
    path: root.runtimeDir + "/events.jsonl"
    watchChanges: true
    printErrors: false
    onLoaded: {
      var events = Model.parseEvents(text())
      var latest = Model.latestPlaying(events)
      root.subtitle = latest && latest.subtitle ? latest.subtitle : null
      // Under --auto-next the episode on screen moves on; the label follows.
      if (latest && root.playing) root.playing = Model.withLatestEpisode(root.playing, latest)
      if (root.playRequested) {
        var error = Model.latestError(events)
        if (error) { root.lastError = error; root.playRequested = false }
        else if (latest) root.playRequested = false
      }
    }
  }

  property var playerSocket: null
  function armSocket() {
    root.releaseSocket()
    var s = socketComponent.createObject(root)
    if (s === null) return
    root.playerSocket = s
    s.connected = true
    if (!s.connected) { root.playerSocket = null; s.destroy(); socketRetry.restart() }
  }
  function releaseSocket() {
    socketRetry.stop()
    if (root.playerSocket) { var s = root.playerSocket; root.playerSocket = null; s.connected = false; s.destroy() }
  }
  function socketWrite(command) {
    var s = root.playerSocket
    if (!s || !s.connected) return false
    s.write(JSON.stringify({ command: command }) + "\n")
    if (s.flush) s.flush()
    return true
  }
  Component {
    id: socketComponent
    Socket {
      path: root.playing && root.playing.socket ? String(root.playing.socket) : root.runtimeDir + "/mpv.sock"
      onConnectionStateChanged: {
        if (this.connected) {
          this.write(JSON.stringify({ command: ["observe_property", 1, "pause"] }) + "\n")
          this.write(JSON.stringify({ command: ["observe_property", 2, "time-pos"] }) + "\n")
          this.write(JSON.stringify({ command: ["observe_property", 3, "duration"] }) + "\n")
          if (this.flush) this.flush()
          return
        }
        if (root.playerSocket !== this) return
        // mpv went away: the supervisor clears now-playing.json shortly after.
        root.playerSocket = null
        this.destroy()
        settleTimer.restart()
      }
      parser: SplitParser {
        splitMarker: "\n"
        onRead: function(line) {
          var next = Model.mpvLine({ paused: root.paused, position: root.position, duration: root.duration }, String(line))
          if (next.paused !== root.paused) root.paused = next.paused
          if (next.position !== root.position) root.position = next.position
          if (next.duration !== root.duration) root.duration = next.duration
        }
      }
    }
  }
  // mpv takes a moment to create its socket after ani-py resolves the stream.
  Timer { id: socketRetry; interval: 1000; repeat: true; running: false; onTriggered: { if (root.playing && !root.playerSocket) root.armSocket(); else socketRetry.stop() } }
  Timer { id: settleTimer; interval: 1500; onTriggered: root.refreshStatus() }
  // Idle poll: catches playbacks started from the Omarchy menu and finished downloads.
  Timer { interval: root.isPlaying || root.activeDownloads > 0 ? 5000 : 10000; repeat: true; running: true; onTriggered: root.refreshStatus() }

  Component.onCompleted: {
    root.refreshSettings()
    root.refreshStatus()
    root.refreshHome(null)
  }
}
