const test = require("node:test")
const assert = require("node:assert/strict")
const Model = require("../SenpaiModel.js")

test("settings merge the manifest defaults with the bar entry and coerce types", () => {
  const s = Model.settingsFrom({ id: "io.github.ferc10110.senpai", quality: "720", dub: "true", barLabelMaxWidth: "40", autoNext: 1 })
  assert.equal(s.quality, "720")
  assert.equal(s.dub, true)
  assert.equal(s.autoNext, true)
  assert.equal(s.barLabelMaxWidth, 60)        // clamped to the minimum
  assert.equal(s.subLang, "latino,es,en")     // default kept
  assert.equal(s.provider, "auto")
  assert.deepEqual(Model.settingsFrom(null), Model.DEFAULTS)
})

test("findBarEntry looks through every section and tolerates bare ids", () => {
  const cfg = { layout: { left: ["omarchy.menu"], center: [], right: [{ id: "io.github.ferc10110.senpai", quality: "480" }] } }
  assert.deepEqual(Model.findBarEntry(cfg, "io.github.ferc10110.senpai"), { id: "io.github.ferc10110.senpai", quality: "480" })
  assert.deepEqual(Model.findBarEntry({ layout: { left: ["io.github.ferc10110.senpai"] } }, "io.github.ferc10110.senpai"), { id: "io.github.ferc10110.senpai" })
  assert.deepEqual(Model.findBarEntry(null, "x"), {})
})

test("entryFrom writes every setting next to the id", () => {
  const entry = Model.entryFrom(Object.assign({}, Model.DEFAULTS, { quality: "1080" }), "io.github.ferc10110.senpai")
  assert.equal(entry.id, "io.github.ferc10110.senpai")
  assert.equal(entry.quality, "1080")
  assert.equal(entry.subLang, "latino,es,en")
})

test("cycle wraps and recovers from an unknown value", () => {
  assert.equal(Model.cycle(Model.PROVIDERS, "auto"), "hianime")
  assert.equal(Model.cycle(Model.PROVIDERS, "animeflv"), "auto")
  assert.equal(Model.cycle(Model.QUALITIES, "nope"), "best")
})

test("nextEpisode continues after a half episode", () => {
  assert.equal(Model.nextEpisode("12.5"), "13")
  assert.equal(Model.nextEpisode("7"), "8")
  assert.equal(Model.nextEpisode("ova"), "ova")
})

test("formatTime renders minutes and hours", () => {
  assert.equal(Model.formatTime(0), "0:00")
  assert.equal(Model.formatTime(754.6), "12:34")
  assert.equal(Model.formatTime(3725), "1:02:05")
  assert.equal(Model.formatTime(-3), "0:00")
  assert.equal(Model.formatTime(NaN), "0:00")
})

test("bar label keeps unicode and the episode", () => {
  assert.equal(Model.barLabel({ title: "Dan Da Dan", episode: "5" }), "Dan Da Dan · 5")
  assert.equal(Model.barLabel({ title: "Frieren: Beyond Journey's End", episode: "12.5" }), "Frieren: Beyond Journey's End · 12.5")
  assert.equal(Model.barLabel(null), "")
})

test("tooltip says what is playing or that nothing is", () => {
  assert.equal(Model.tooltip({ playing: null, downloads: 0 }), "Senpai: nothing playing")
  assert.equal(
    Model.tooltip({ playing: { title: "One Piece", episode: "4" }, paused: true, position: 65, duration: 1440, subtitle: { language: "es", source: "animetosho" }, downloads: 2 }),
    "One Piece · Ep 4 · 1:05 / 24:00 · paused · subtitles es (animetosho) · 2 downloads running",
  )
})

test("home rows put continue first, then recents, or an empty hint", () => {
  const home = {
    continue: { provider: "hianime", id: "one-piece-100", title: "One Piece", episode: "5", resume: false },
    recent: [
      { provider: "hianime", id: "one-piece-100", title: "One Piece", episode: "4", completed: true },
      { provider: "hianime", id: "frieren-481", title: "Frieren", episode: "7", completed: false },
    ],
  }
  const rows = Model.homeRows(home)
  assert.equal(rows[0].kind, "continue")
  assert.equal(rows[0].title, "Continue · One Piece")
  assert.equal(rows[0].subtitle, "Episode 5")
  assert.equal(rows[1].kind, "recent")
  assert.equal(rows[1].subtitle, "Episode 4 · watched")
  assert.equal(rows[2].subtitle, "Episode 7 · unfinished")
  const resume = Model.homeRows({ continue: Object.assign({}, home.continue, { resume: true }), recent: [] })
  assert.equal(resume[0].subtitle, "Episode 5 · resume")
  assert.deepEqual(Model.homeRows({ continue: null, recent: [] }).map(r => r.kind), ["empty"])
})

test("result and episode rows carry what the UI shows", () => {
  const results = Model.resultRows({ results: [{ provider: "animeav1", id: "frieren", title: "Frieren" }] })
  assert.deepEqual(results[0], { kind: "result", provider: "animeav1", id: "frieren", title: "Frieren", subtitle: "animeav1" })
  const episodes = Model.episodeRows({ anime: { provider: "hianime", id: "x", title: "X" }, episodes: [
    { number: "1", id: "1001", state: "watched" }, { number: "2", id: "1002", state: "unfinished" }, { number: "3", id: "1003", state: "new" },
  ] })
  assert.deepEqual(episodes.map(e => e.mark), ["✓", "◐", ""])
  assert.equal(episodes[1].title, "Episode 2")
  assert.equal(episodes[1].number, "2")
})

test("jumpIndex prefers the exact number, then a prefix", () => {
  const rows = ["1", "2", "10", "11", "100", "101"].map(n => ({ number: n }))
  assert.equal(Model.jumpIndex(rows, "10"), 2)
  assert.equal(Model.jumpIndex(rows, "1"), 0)
  assert.equal(Model.jumpIndex(rows, "10x"), -1)
  assert.equal(Model.jumpIndex(rows, "9"), -1)
  assert.equal(Model.jumpIndex(rows, ""), -1)
})

test("clampIndex stays inside the list", () => {
  assert.equal(Model.clampIndex(-1, 5), 0)
  assert.equal(Model.clampIndex(9, 5), 4)
  assert.equal(Model.clampIndex(2, 0), 0)
})

test("events are parsed leniently and the latest playing survives until ended", () => {
  const text = '{"event":"resolving","episode":"4"}\nnot json\n{"event":"playing","episode":"4","title":"One Piece","subtitle":{"language":"es","label":"Spanish","source":"animetosho"}}\n'
  const events = Model.parseEvents(text)
  assert.equal(events.length, 2)
  assert.deepEqual(Model.latestPlaying(events).subtitle, { language: "es", label: "Spanish", source: "animetosho" })
  assert.equal(Model.latestPlaying(events.concat([{ event: "ended", rc: 0 }])), null)
  assert.equal(Model.latestPlaying([]), null)
})

test("mpv property changes update pause, position and duration", () => {
  let s = { paused: false, position: 0, duration: 0 }
  s = Model.mpvLine(s, '{"event":"property-change","id":1,"name":"pause","data":true}')
  s = Model.mpvLine(s, '{"event":"property-change","id":2,"name":"time-pos","data":12.7}')
  s = Model.mpvLine(s, '{"event":"property-change","id":3,"name":"duration","data":1440.2}')
  assert.deepEqual(s, { paused: true, position: 12.7, duration: 1440.2 })
  assert.deepEqual(Model.mpvLine(s, "garbage"), s)
  assert.deepEqual(Model.mpvLine(s, '{"event":"property-change","name":"time-pos","data":null}'), s)
})

test("playArgs turns settings into helper flags", () => {
  const args = Model.playArgs(Object.assign({}, Model.DEFAULTS, { dub: true, quality: "720", autoNext: true, subSearch: false, skipIntro: true, downloadDir: "/tmp/dl", aniPyPath: "/opt/ani_py.py" }))
  assert.deepEqual(args, ["--sub-lang", "latino,es,en", "--quality", "720", "--dub", "--auto-next", "--no-sub-search", "--skip", "--download-dir", "/tmp/dl"])
  assert.deepEqual(Model.playArgs(Model.DEFAULTS), ["--sub-lang", "latino,es,en", "--quality", "best"])
})

test("downloads summary", () => {
  const list = [{ state: "running", title: "A", episodes: "1-3" }, { state: "done", title: "B", episodes: "4" }, { state: "failed", title: "C", episodes: "5" }]
  assert.equal(Model.countRunning(list), 1)
  assert.equal(Model.downloadLine(list[0]), "↓ A 1-3 · downloading")
  assert.equal(Model.downloadLine(list[1]), "↓ B 4 · done")
  assert.equal(Model.downloadLine(list[2]), "↓ C 5 · failed")
})

test("now playing line", () => {
  assert.equal(Model.nowPlayingLine({ title: "One Piece", episode: "4" }, false, 65, 1440), "▶ One Piece · Ep 4 · 1:05 / 24:00")
  assert.equal(Model.nowPlayingLine({ title: "One Piece", episode: "4" }, true, 65, 0), "⏸ One Piece · Ep 4 · 1:05")
  assert.equal(Model.nowPlayingLine(null, false, 0, 0), "")
})

test("glyphs are single Nerd Font code points", () => {
  for (const g of [Model.GLYPH_IDLE, Model.GLYPH_PLAYING, Model.GLYPH_PAUSED]) assert.equal([...g].length, 1)
})

test("cursorAfterEpisodes lands on the first episode still to watch", () => {
  const rows = [{ state: "watched" }, { state: "watched" }, { state: "unfinished" }, { state: "new" }]
  assert.equal(Model.cursorAfterEpisodes(rows), 2)
  assert.equal(Model.cursorAfterEpisodes([{ state: "watched" }, { state: "watched" }]), 1)
  assert.equal(Model.cursorAfterEpisodes([]), 0)
})

test("chips describe the settings in order", () => {
  const chips = Model.chips(Object.assign({}, Model.DEFAULTS, { dub: true, quality: "720", autoNext: true, subSearch: false }))
  assert.deepEqual(chips, ["Provider auto", "Audio dub", "Quality 720", "Subs off", "Auto-next on"])
  assert.deepEqual(Model.chips(Model.DEFAULTS), ["Provider auto", "Audio sub", "Quality best", "Subs on", "Auto-next off"])
})
