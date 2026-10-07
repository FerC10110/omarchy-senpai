// SenpaiModel.js -- pure logic for io.github.ferc10110.senpai.
// No QML, no I/O, no timers: values in, values out. ES5 so the QML engine
// and node both load it; tests live in tests/model.test.js.

var PLUGIN_ID = "io.github.ferc10110.senpai"
var PROVIDERS = ["auto", "hianime", "animeav1", "animeflv"]
var QUALITIES = ["best", "1080", "720", "480", "360", "worst"]
var LABEL_WIDTH_MIN = 60
var LABEL_WIDTH_MAX = 600

// Nerd Font (Font Awesome set): film, play-circle, pause-circle.
var GLYPH_IDLE = ""
var GLYPH_PLAYING = ""
var GLYPH_PAUSED = ""

var GLYPH_LATER = ""   // bookmark: kept for later
var GLYPH_SEARCH = ""  // magnifier, in the search box

var DEFAULTS = {
  subLang: "latino,es,en",
  provider: "auto",
  dub: false,
  quality: "best",
  autoNext: false,
  subSearch: true,
  skipIntro: false,
  downloadDir: "",
  showTitleInBar: true,
  barLabelMaxWidth: 180,
  aniPyPath: ""
}

// Alt+S presets; a custom subLang list is not in here, so it cycles back to the default.
var SUB_LANGS = ["latino,es,en", "es,en", "en"]

function str(v) { return v === undefined || v === null ? "" : String(v) }

function boolSetting(v) {
  if (v === true || v === 1) return true
  var s = str(v).toLowerCase()
  return s === "true" || s === "1" || s === "yes" || s === "on"
}

function intSetting(v, fallback, min, max) {
  var n = parseInt(v, 10)
  if (isNaN(n)) n = fallback
  if (n < min) n = min
  if (n > max) n = max
  return n
}

function findBarEntry(barConfig, pluginId) {
  var id = str(pluginId)
  if (!barConfig || typeof barConfig !== "object" || !barConfig.layout || typeof barConfig.layout !== "object") return {}
  var sections = ["left", "center", "right"]
  for (var s = 0; s < sections.length; s++) {
    var entries = barConfig.layout[sections[s]]
    if (!Array.isArray(entries)) continue
    for (var i = 0; i < entries.length; i++) {
      var entry = entries[i]
      if (typeof entry === "string" && entry === id) return { id: id }
      if (entry && typeof entry === "object" && str(entry.id) === id) return entry
    }
  }
  return {}
}

function settingsFrom(entry) {
  var e = entry && typeof entry === "object" ? entry : {}
  function pick(key) { return e[key] === undefined || e[key] === null || e[key] === "" ? DEFAULTS[key] : e[key] }
  return {
    subLang: str(pick("subLang")),
    provider: PROVIDERS.indexOf(str(pick("provider"))) >= 0 ? str(pick("provider")) : DEFAULTS.provider,
    dub: boolSetting(pick("dub")),
    quality: str(pick("quality")),
    autoNext: boolSetting(pick("autoNext")),
    subSearch: boolSetting(pick("subSearch")),
    skipIntro: boolSetting(pick("skipIntro")),
    downloadDir: str(e.downloadDir || ""),
    showTitleInBar: boolSetting(pick("showTitleInBar")),
    barLabelMaxWidth: intSetting(pick("barLabelMaxWidth"), DEFAULTS.barLabelMaxWidth, LABEL_WIDTH_MIN, LABEL_WIDTH_MAX),
    aniPyPath: str(e.aniPyPath || "")
  }
}

function entryFrom(settings, pluginId) {
  var entry = { id: str(pluginId) }
  var keys = Object.keys(DEFAULTS)
  for (var i = 0; i < keys.length; i++) entry[keys[i]] = settings[keys[i]]
  return entry
}

// Same values for every settings key (both are settingsFrom() objects).
function settingsEqual(a, b) {
  if (!a || !b) return false
  var keys = Object.keys(DEFAULTS)
  for (var i = 0; i < keys.length; i++) if (a[keys[i]] !== b[keys[i]]) return false
  return true
}

function cycle(list, current) {
  var i = list.indexOf(current)
  return list[(i + 1) % list.length]
}

function nextEpisode(number) {
  var n = parseFloat(number)
  if (isNaN(n)) return str(number)
  return String(Math.floor(n) + 1)
}

function formatTime(seconds) {
  var total = Math.floor(Number(seconds))
  if (!isFinite(total) || total < 0) total = 0
  var h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60
  var mm = (h > 0 && m < 10 ? "0" : "") + m, ss = (s < 10 ? "0" : "") + s
  return h > 0 ? h + ":" + mm + ":" + ss : m + ":" + ss
}

function barLabel(playing) {
  if (!playing) return ""
  return str(playing.title) + " · " + str(playing.episode)
}

function tooltip(state) {
  var downloads = state.downloads || 0
  if (!state.playing) return "Senpai: nothing playing" + (downloads ? " · " + downloads + " download" + (downloads === 1 ? "" : "s") + " running" : "")
  var parts = [str(state.playing.title) + " · Ep " + str(state.playing.episode)]
  var clock = formatTime(state.position)
  if (state.duration > 0) clock += " / " + formatTime(state.duration)
  parts.push(clock)
  if (state.paused) parts.push("paused")
  if (state.subtitle && state.subtitle.language) parts.push("subtitles " + state.subtitle.language + (state.subtitle.source ? " (" + state.subtitle.source + ")" : ""))
  if (downloads) parts.push(downloads + " download" + (downloads === 1 ? "" : "s") + " running")
  return parts.join(" · ")
}

// Home: three sections under small headers; a header row is never selectable.
function homeRows(home) {
  var rows = []
  var cont = home && home.continue ? home.continue : null
  if (cont) {
    rows.push({ kind: "header", title: "Continue" })
    rows.push({ kind: "continue", provider: cont.provider, id: cont.id, title: str(cont.title),
                animeTitle: str(cont.title), episode: str(cont.episode),
                subtitle: "Episode " + str(cont.episode) + (cont.resume ? " · resume" : "") })
  }
  var later = home && Array.isArray(home.later) ? home.later : []
  if (later.length > 0) rows.push({ kind: "header", title: "Watch later" })
  for (var l = 0; l < later.length; l++) {
    var k = later[l]
    rows.push({ kind: "later", provider: str(k.provider), id: str(k.id), title: str(k.title), animeTitle: str(k.title), subtitle: str(k.provider) })
  }
  var recent = home && Array.isArray(home.recent) ? home.recent : []
  if (recent.length > 0) rows.push({ kind: "header", title: "Recent" })
  for (var i = 0; i < recent.length; i++) {
    var r = recent[i]
    rows.push({ kind: "recent", provider: r.provider, id: r.id, title: str(r.title), animeTitle: str(r.title), episode: str(r.episode),
                subtitle: "Episode " + str(r.episode) + " · " + (r.completed ? "watched" : "unfinished") })
  }
  if (rows.length === 0) rows.push({ kind: "empty", title: "Nothing yet", subtitle: "Type to search · Ctrl+W keeps an anime for later" })
  return rows
}

// "provider:id" of every anime kept for later, for marking search results.
function laterKeys(home) {
  var keys = {}
  var later = home && Array.isArray(home.later) ? home.later : []
  for (var i = 0; i < later.length; i++) keys[str(later[i].provider) + ":" + str(later[i].id)] = true
  return keys
}

function resultRows(payload, keys) {
  var list = payload && Array.isArray(payload.results) ? payload.results : []
  return list.map(function (r) {
    var later = !!(keys && keys[str(r.provider) + ":" + str(r.id)])
    return { kind: "result", provider: str(r.provider), id: str(r.id), title: str(r.title), subtitle: str(r.provider), later: later }
  })
}

function episodeMark(state) {
  if (state === "watched") return "✓"
  if (state === "unfinished") return "◐"
  return ""
}

function episodeRows(payload) {
  var list = payload && Array.isArray(payload.episodes) ? payload.episodes : []
  return list.map(function (e) {
    return { kind: "episode", number: str(e.number), id: str(e.id), state: str(e.state), mark: episodeMark(e.state), title: "Episode " + str(e.number) }
  })
}

function jumpIndex(rows, digits) {
  var d = str(digits)
  if (d === "" || !/^\d+$/.test(d)) return -1
  var prefix = -1
  for (var i = 0; i < rows.length; i++) {
    var n = str(rows[i].number)
    if (n === d) return i
    if (prefix < 0 && n.indexOf(d) === 0) prefix = i
  }
  return prefix
}

function clampIndex(index, count) {
  if (count <= 0) return 0
  if (index < 0) return 0
  if (index >= count) return count - 1
  return index
}

function parseEvents(text) {
  var out = []
  var lines = str(text).split("\n")
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trim()
    if (!line) continue
    try {
      var obj = JSON.parse(line)
      if (obj && typeof obj === "object" && obj.event) out.push(obj)
    } catch (e) { /* a half-written line; the next read sees it whole */ }
  }
  return out
}

function latestPlaying(events) {
  var found = null
  for (var i = 0; i < events.length; i++) {
    if (events[i].event === "playing") found = events[i]
    else if (events[i].event === "ended") found = null
  }
  return found
}

function withLatestEpisode(playing, latest) {
  if (!playing || !latest || !latest.episode) return playing
  var next = {}
  for (var key in playing) if (Object.prototype.hasOwnProperty.call(playing, key)) next[key] = playing[key]
  next.episode = str(latest.episode)
  if (latest.title) next.title = str(latest.title)
  return next
}

function latestError(events) {
  var found = ""
  for (var i = 0; i < events.length; i++) {
    if (events[i].event === "error" && events[i].message) found = str(events[i].message)
  }
  return found
}

function mpvLine(state, line) {
  var msg
  try { msg = JSON.parse(line) } catch (e) { return state }
  if (!msg || msg.event !== "property-change" || msg.data === null || msg.data === undefined) return state
  if (msg.name === "pause") return { paused: !!msg.data, position: state.position, duration: state.duration }
  if (msg.name === "time-pos") return { paused: state.paused, position: Number(msg.data), duration: state.duration }
  if (msg.name === "duration") return { paused: state.paused, position: state.position, duration: Number(msg.data) }
  return state
}

function playArgs(settings) {
  var args = ["--sub-lang", settings.subLang, "--quality", settings.quality]
  if (settings.dub) args.push("--dub")
  if (settings.autoNext) args.push("--auto-next")
  if (!settings.subSearch) args.push("--no-sub-search")
  if (settings.skipIntro) args.push("--skip")
  if (settings.downloadDir) args.push("--download-dir", settings.downloadDir)
  return args
}

function countRunning(downloads) {
  var n = 0
  for (var i = 0; i < (downloads || []).length; i++) if (downloads[i].state === "running") n++
  return n
}

function downloadLine(d) {
  var state = d.state === "running" ? "downloading" : str(d.state)
  return "↓ " + str(d.title) + " " + str(d.episodes) + " · " + state
}

function nowPlayingLine(playing, paused, position, duration) {
  if (!playing) return ""
  var clock = formatTime(position)
  if (duration > 0) clock += " / " + formatTime(duration)
  return (paused ? "⏸ " : "▶ ") + str(playing.title) + " · Ep " + str(playing.episode) + " · " + clock
}

function cursorAfterEpisodes(rows) {
  for (var i = 0; i < rows.length; i++) if (rows[i].state !== "watched") return i
  return rows.length > 0 ? rows.length - 1 : 0
}

function selectable(row) {
  return !!row && row.kind !== "header" && row.kind !== "empty"
}

function firstSelectable(rows) {
  for (var i = 0; i < rows.length; i++) if (selectable(rows[i])) return i
  return 0
}

// The cursor after moving `delta` rows from `from`, skipping headers; it never
// leaves the list, and comes back toward `from` when it would run off the end.
function stepCursor(rows, from, delta) {
  if (!rows || rows.length === 0) return 0
  var dir = delta < 0 ? -1 : 1
  var target = clampIndex(from + delta, rows.length)
  var i = target
  while (i >= 0 && i < rows.length && !selectable(rows[i])) i += dir
  if (i < 0 || i >= rows.length) {
    i = target
    while (i >= 0 && i < rows.length && !selectable(rows[i])) i -= dir
  }
  return i >= 0 && i < rows.length && selectable(rows[i]) ? i : from
}

var SETTINGS_HINT = "Alt+P/A/Q/S/N settings"
var PLAYBACK_HINT = "Ctrl+Space pause · Ctrl+N next · Ctrl+S stop"

// The footer line: only the keys that do something in this mode.
function keyHints(mode, playing) {
  var hints
  if (mode === "episodes") hints = "Enter play · Ctrl+D save · Ctrl+Shift+D range · Ctrl+W later · Esc back"
  else if (mode === "range") hints = "Enter save · Esc back"
  else if (mode === "results") hints = "Enter episodes · Ctrl+W later · Esc home · " + SETTINGS_HINT
  else hints = "Enter open · Ctrl+W later · Esc close · " + SETTINGS_HINT
  return playing ? hints + " · " + PLAYBACK_HINT : hints
}

// The glyph column of a row: play, bookmark, watched, unfinished.
function rowGlyph(row) {
  if (!row) return ""
  if (row.kind === "continue") return "▶"
  if (row.kind === "later" || row.later === true) return GLYPH_LATER
  if (row.kind === "episode") return episodeMark(row.state)
  return ""
}

// The first language of the subLang list, which is what mostly plays.
function subsLabel(subLang) {
  var first = str(subLang).split(",")[0].trim()
  return first === "" ? "auto" : first
}

function chips(settings) {
  return [
    "Provider " + settings.provider,
    "Audio " + (settings.dub ? "dub" : "sub"),
    "Quality " + settings.quality,
    "Subs " + subsLabel(settings.subLang),
    "Search " + (settings.subSearch ? "on" : "off"),
    "Auto-next " + (settings.autoNext ? "on" : "off")
  ]
}

if (typeof module !== "undefined") {
  module.exports = {
    PLUGIN_ID: PLUGIN_ID, PROVIDERS: PROVIDERS, QUALITIES: QUALITIES, SUB_LANGS: SUB_LANGS, DEFAULTS: DEFAULTS,
    GLYPH_IDLE: GLYPH_IDLE, GLYPH_PLAYING: GLYPH_PLAYING, GLYPH_PAUSED: GLYPH_PAUSED, GLYPH_LATER: GLYPH_LATER, GLYPH_SEARCH: GLYPH_SEARCH,
    laterKeys: laterKeys, selectable: selectable, firstSelectable: firstSelectable, stepCursor: stepCursor,
    keyHints: keyHints, rowGlyph: rowGlyph,
    boolSetting: boolSetting, intSetting: intSetting, findBarEntry: findBarEntry, settingsFrom: settingsFrom,
    entryFrom: entryFrom, settingsEqual: settingsEqual, cycle: cycle, nextEpisode: nextEpisode, formatTime: formatTime, barLabel: barLabel,
    tooltip: tooltip, homeRows: homeRows, resultRows: resultRows, episodeMark: episodeMark, episodeRows: episodeRows,
    jumpIndex: jumpIndex, clampIndex: clampIndex, parseEvents: parseEvents, latestPlaying: latestPlaying,
    mpvLine: mpvLine, playArgs: playArgs, countRunning: countRunning, downloadLine: downloadLine,
    nowPlayingLine: nowPlayingLine, cursorAfterEpisodes: cursorAfterEpisodes, chips: chips,
    withLatestEpisode: withLatestEpisode, latestError: latestError
  }
}
