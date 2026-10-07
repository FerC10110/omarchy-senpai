import Quickshell
import Quickshell.Wayland
import QtQuick
import qs.Commons
import qs.Ui
import "SenpaiModel.js" as Model

// Senpai.qml -- the finder overlay of io.github.ferc10110.senpai.
// Four modes: home (continue + recents), results (a search), episodes
// (one anime), range (typing "1-12" to download several). Every key goes
// through keyHost.Keys.onPressed; typing edits the query or the range,
// digits in episodes mode jump to an episode. State lives in Service.qml;
// this file only renders and dispatches. Dismiss via shell.hide(pluginId).
Item {
  id: root

  property var shell: null
  property var manifest: null
  property var service: null
  property bool opened: false
  readonly property string pluginId: manifest && manifest.id ? String(manifest.id) : Model.PLUGIN_ID
  readonly property bool serviceReady: service !== null

  // ---- finder state
  property string mode: "home"           // home | results | episodes | range
  property string query: ""
  property string rangeText: ""
  property string digits: ""
  property var rows: []
  property int cursor: 0
  property var anime: null               // { provider, id, title }
  property string cameFrom: "home"       // where Escape goes from episodes
  property string notice: ""

  readonly property bool busy: serviceReady && service.busy === true
  readonly property string errorText: serviceReady ? String(service.lastError || "") : ""
  readonly property var settings: serviceReady ? service.settings : Model.DEFAULTS
  readonly property bool playing: serviceReady && service.isPlaying === true

  // ---- theme
  readonly property color background: Color.menu.background
  readonly property color foreground: Color.menu.text
  readonly property color dim: Util.alpha(Color.menu.text, 0.55)
  readonly property color accent: Color.menu.selectedText
  readonly property color selectedBackground: Color.menu.selectedBackground
  readonly property var borderSpec: Border.surfaceSpec("menu", "border", Color.menu.border, Math.max(1, Style.space(2)))
  readonly property string fontFamily: Style.font.menuFamily
  readonly property int cardWidth: Math.min(Style.space(760), panel.width - Style.gapsOut * 2)
  readonly property int cardHeight: Math.min(Style.space(560), panel.height - Style.gapsOut * 2)
  readonly property int rowHeight: Math.max(Style.space(40), Style.font.title + Style.font.bodySmall + Style.space(10))
  readonly property int headerHeight: Style.font.caption + Style.space(14)

  // ---- host API
  function resolveService() {
    if (root.service) return
    if (root.shell && typeof root.shell.serviceFor === "function") root.service = root.shell.serviceFor(root.pluginId)
  }

  function open(payloadJson) {
    root.resolveService()
    var payload = {}
    try { payload = JSON.parse(payloadJson || "{}") || {} } catch (e) { payload = {} }
    root.notice = ""
    root.digits = ""
    root.rangeText = ""
    root.anime = null
    if (typeof payload.query === "string" && payload.query !== "") {
      root.query = payload.query
      root.mode = "results"
      root.rows = []
      searchTimer.restart()
    } else {
      root.query = ""
      root.showHome()
    }
    root.opened = true
    if (root.serviceReady) {
      root.service.refreshStatus()
      root.service.refreshHome(function() { if (root.mode === "home") root.showHome() })
    }
    root.refocus()
  }

  function close() {
    root.opened = false
    searchTimer.stop()
    digitTimer.stop()
    noticeTimer.stop()
  }

  function dismiss() {
    root.close()
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  function refocus() { Qt.callLater(function() { keyHost.forceActiveFocus() }) }

  // ---- test hooks (omarchy-shell shell call <id> <method> <arg>)
  function setQuery(text) { root.setQueryText(String(text || "")); return "ok" }
  function stateJson() {
    return JSON.stringify({ mode: root.mode, query: root.query, cursor: root.cursor, rows: root.rows.length,
                            anime: root.anime, busy: root.busy, error: root.errorText,
                            first: root.rows.length > 0 ? String(root.rows[0].title || "") : "",
                            playing: root.playing ? root.service.playing : null,
                            paused: root.playing ? root.service.paused : false,
                            position: root.playing ? root.service.position : 0,
                            duration: root.playing ? root.service.duration : 0,
                            kinds: root.rows.map(function(r) { return r.kind }),
                            cursorKind: root.current() ? String(root.current().kind) : "",
                            later: root.serviceReady && root.service.home && Array.isArray(root.service.home.later) ? root.service.home.later.length : 0,
                            settings: root.settings,
                            ownSettings: root.serviceReady && root.service.ownSettings !== null,
                            hostQuality: root.serviceReady ? root.service.hostSettings.quality : "" })
  }
  function pressKey(name) {
    var n = String(name || "")
    if (n.indexOf("alt+") === 0) return root.settingShortcut(n.slice(4)) ? "ok" : "unknown"
    if (n === "up") root.moveCursor(-1)
    else if (n === "down") root.moveCursor(1)
    else if (n === "enter") root.activate()
    else if (n === "escape") root.back()
    else if (n === "ctrl+d") root.downloadHighlighted()
    else if (n === "ctrl+w") root.toggleLaterHighlighted()
    else if (n === "ctrl+space") { if (root.serviceReady) root.service.togglePause() }
    else if (n === "ctrl+n") { if (root.serviceReady) root.service.next() }
    else if (n === "ctrl+s") { if (root.serviceReady) root.service.stop() }
    else return "unknown"
    return "ok"
  }

  // ---- modes
  function showHome() {
    root.mode = "home"
    root.rows = Model.homeRows(root.serviceReady ? root.service.home : null)
    root.cursor = Model.firstSelectable(root.rows)
    root.scrollToCursor()
  }

  // Re-render the current list after the watch-later list changed.
  function refreshRows() {
    if (!root.serviceReady) return
    if (root.mode === "home") {
      var was = root.cursor
      root.rows = Model.homeRows(root.service.home)
      root.cursor = Model.stepCursor(root.rows, Model.clampIndex(was, root.rows.length), 0)
      root.scrollToCursor()
    } else if (root.mode === "results") {
      root.rows = Model.resultRows({ results: root.rows }, Model.laterKeys(root.service.home))
    }
  }

  function setQueryText(text) {
    root.query = text
    if (root.mode === "episodes" || root.mode === "range") return
    if (text === "") { searchTimer.stop(); root.showHome(); return }
    root.mode = "results"
    searchTimer.restart()
  }

  function runSearch() {
    if (!root.serviceReady || root.query === "") return
    var asked = root.query
    root.service.search(asked, function(payload) {
      if (root.query !== asked || root.mode !== "results") return
      root.rows = Model.resultRows(payload, Model.laterKeys(root.service.home))
      root.cursor = 0
      root.scrollToCursor()
      if (payload && root.rows.length === 0) root.showNotice("No results for \"" + asked + "\"")
    })
  }

  function openEpisodes(provider, id, title, from) {
    if (!root.serviceReady) return
    root.cameFrom = from
    root.anime = { provider: provider, id: id, title: title }
    root.service.episodes(provider, id, title, function(payload) {
      if (!root.anime || root.anime.id !== id) return
      if (!payload) { root.anime = null; return }
      root.mode = "episodes"
      root.digits = ""
      root.rows = Model.episodeRows(payload)
      root.cursor = Model.cursorAfterEpisodes(root.rows)
      // Centered, so the episodes around the next one are in view.
      if (list.height > 0) list.positionViewAtIndex(root.cursor, ListView.Center)
    })
  }

  function back() {
    if (root.mode === "range") { root.mode = "episodes"; root.rangeText = ""; return }
    if (root.mode === "episodes") {
      root.anime = null
      root.digits = ""
      if (root.cameFrom === "results" && root.query !== "") { root.mode = "results"; searchTimer.stop(); root.runSearch(); return }
      root.query = ""
      root.showHome()
      return
    }
    if (root.mode === "results") { root.query = ""; root.showHome(); return }
    root.dismiss()
  }

  // ---- cursor
  function moveCursor(delta) {
    root.digits = ""
    root.cursor = Model.stepCursor(root.rows, root.cursor, delta)
    root.scrollToCursor()
  }
  function pageSize() { return Math.max(1, Math.floor(list.height / root.rowHeight) - 1) }
  function scrollToCursor() {
    if (root.rows.length > 0 && list.height > 0) list.positionViewAtIndex(root.cursor, ListView.Contain)
  }

  // ---- actions
  function current() { return root.rows.length > 0 ? root.rows[Model.clampIndex(root.cursor, root.rows.length)] : null }

  function activate() {
    var row = root.current()
    if (!row || !root.serviceReady) return
    if (root.mode === "home") {
      if (row.kind === "continue") { root.service.play(row.provider, row.id, row.animeTitle, row.episode); root.dismiss(); return }
      if (row.kind === "recent" || row.kind === "later") { root.openEpisodes(row.provider, row.id, row.animeTitle, "home"); return }
      return
    }
    if (root.mode === "results") { root.openEpisodes(row.provider, row.id, row.title, "results"); return }
    if (root.mode === "episodes") {
      root.service.play(root.anime.provider, root.anime.id, root.anime.title, row.number)
      root.dismiss()
      return
    }
    if (root.mode === "range") {
      var spec = root.rangeText.trim()
      if (spec === "") return
      root.service.download(root.anime.provider, root.anime.id, root.anime.title, spec)
      root.showNotice("Downloading " + root.anime.title + " " + spec)
      root.rangeText = ""
      root.mode = "episodes"
    }
  }

  // Ctrl+W: keep the highlighted anime (or the open one) for later, or drop it.
  function toggleLaterHighlighted() {
    if (!root.serviceReady) return
    var target, keep
    if (root.mode === "episodes" || root.mode === "range") {
      if (!root.anime) return
      target = root.anime
      keep = !root.service.isLater(target.provider, target.id)
    } else {
      var row = root.current()
      if (!row || !Model.selectable(row)) return
      target = { provider: row.provider, id: row.id, title: row.animeTitle || row.title }
      keep = row.kind !== "later" && row.later !== true && !root.service.isLater(row.provider, row.id)
    }
    root.service.setLater(target.provider, target.id, target.title, keep, function(payload) {
      if (payload) root.showNotice((keep ? "Kept for later · " : "Dropped from Watch later · ") + target.title)
      root.refreshRows()
    })
  }

  function downloadHighlighted() {
    var row = root.current()
    if (!row || !root.serviceReady || root.mode !== "episodes") return
    root.service.download(root.anime.provider, root.anime.id, root.anime.title, row.number)
    root.showNotice("Downloading " + root.anime.title + " " + row.number)
  }

  function startRange() {
    if (root.mode !== "episodes") return
    root.rangeText = ""
    root.mode = "range"
  }

  function cycleSetting(key, list) {
    if (!root.serviceReady) return
    root.service.setSetting(key, Model.cycle(list, root.settings[key]))
  }

  // The Alt+<letter> shortcuts; also reachable through the pressKey hook.
  function settingShortcut(letter) {
    if (!root.serviceReady) return false
    if (letter === "p") root.cycleSetting("provider", Model.PROVIDERS)
    else if (letter === "q") root.cycleSetting("quality", Model.QUALITIES)
    else if (letter === "s") root.cycleSetting("subLang", Model.SUB_LANGS)
    else if (letter === "a") root.service.setSetting("dub", !root.settings.dub)
    else if (letter === "n") root.service.setSetting("autoNext", !root.settings.autoNext)
    else return false
    return true
  }

  function showNotice(text) {
    root.notice = text
    noticeTimer.restart()
  }

  function typeDigit(ch) {
    root.digits += ch
    digitTimer.restart()
    var index = Model.jumpIndex(root.rows, root.digits)
    if (index >= 0) { root.cursor = index; root.scrollToCursor() }
  }

  // ---- keys
  function handleKey(event) {
    var ctrl = event.modifiers & Qt.ControlModifier
    var alt = event.modifiers & Qt.AltModifier
    var shift = event.modifiers & Qt.ShiftModifier
    if (event.key === Qt.Key_Escape) { root.back(); return true }
    if (alt && event.key === Qt.Key_P) { root.settingShortcut("p"); return true }
    if (alt && event.key === Qt.Key_Q) { root.settingShortcut("q"); return true }
    if (alt && event.key === Qt.Key_S) { root.settingShortcut("s"); return true }
    if (alt && event.key === Qt.Key_A) { root.settingShortcut("a"); return true }
    if (alt && event.key === Qt.Key_N) { root.settingShortcut("n"); return true }
    if (ctrl && event.key === Qt.Key_Space) { if (root.serviceReady) root.service.togglePause(); return true }
    if (ctrl && event.key === Qt.Key_N) { if (root.serviceReady) root.service.next(); return true }
    if (ctrl && event.key === Qt.Key_S) { if (root.serviceReady) root.service.stop(); return true }
    if (ctrl && shift && event.key === Qt.Key_D) { root.startRange(); return true }
    if (ctrl && event.key === Qt.Key_D) { root.downloadHighlighted(); return true }
    if (ctrl && event.key === Qt.Key_W) { root.toggleLaterHighlighted(); return true }
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.activate(); return true }
    if (event.key === Qt.Key_Down) { root.moveCursor(1); return true }
    if (event.key === Qt.Key_Up) { root.moveCursor(-1); return true }
    if (event.key === Qt.Key_PageDown) { root.moveCursor(root.pageSize()); return true }
    if (event.key === Qt.Key_PageUp) { root.moveCursor(-root.pageSize()); return true }
    if (event.key === Qt.Key_Home) { root.moveCursor(-root.rows.length); return true }
    if (event.key === Qt.Key_End) { root.moveCursor(root.rows.length); return true }
    if (event.key === Qt.Key_Tab) { root.moveCursor(shift ? -1 : 1); return true }

    if (root.mode === "range") {
      if (Util.editsFilter(event, root.rangeText)) { root.rangeText = Util.editedFilter(event, root.rangeText); return true }
      if (event.text && /^[0-9,\- ]$/.test(event.text)) { root.rangeText += event.text; return true }
      return false
    }
    if (root.mode === "episodes") {
      if (event.text && /^[0-9]$/.test(event.text)) { root.typeDigit(event.text); return true }
      if (event.key === Qt.Key_Backspace) { root.back(); return true }
      return false
    }
    if (Util.editsFilter(event, root.query)) { root.setQueryText(Util.editedFilter(event, root.query)); return true }
    if (event.text && event.text.length === 1 && !ctrl && !alt && event.text.charCodeAt(0) >= 32) {
      root.setQueryText(root.query + event.text)
      return true
    }
    return false
  }

  Timer { id: searchTimer; interval: 350; onTriggered: root.runSearch() }
  Timer { id: digitTimer; interval: 2000; onTriggered: root.digits = "" }
  Timer { id: noticeTimer; interval: 4000; onTriggered: root.notice = "" }

  PanelWindow {
    id: panel
    visible: root.opened
    anchors { top: true; bottom: true; left: true; right: true }
    color: "transparent"
    WlrLayershell.namespace: "omarchy-senpai"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore

    Rectangle { anchors.fill: parent; color: Color.menu.scrim }
    MouseArea { anchors.fill: parent; onClicked: root.dismiss() }

    BorderSurface {
      id: card
      width: root.cardWidth
      height: root.cardHeight
      radius: Style.cornerRadius
      anchors.centerIn: parent
      color: root.background
      borderSpec: root.borderSpec
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: {} }

      Item {
        id: keyHost
        anchors.fill: parent
        focus: true
        Keys.onPressed: function(event) { if (root.handleKey(event)) event.accepted = true }

        Column {
          anchors.fill: parent
          spacing: Style.spacing.md

          // Header: the title and a search box (episode jump / range in the other modes).
          Item {
            width: parent.width
            height: searchBox.height
            Text {
              id: prompt
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.mode === "range" ? "Save episodes" : (root.mode === "episodes" && root.anime ? String(root.anime.title) : "Senpai")
              textFormat: Text.PlainText
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
              font.bold: true
              elide: Text.ElideRight
              width: Math.min(implicitWidth, parent.width * 0.45)
            }
            Rectangle {
              id: searchBox
              anchors.left: prompt.right
              anchors.leftMargin: Style.spacing.lg
              anchors.right: busyText.left
              anchors.rightMargin: Style.spacing.md
              anchors.verticalCenter: parent.verticalCenter
              height: Style.font.heading + Style.spacing.controlPaddingY * 2
              radius: Style.cornerRadius
              color: root.selectedBackground
              Text {
                id: searchGlyph
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.controlPaddingX
                anchors.verticalCenter: parent.verticalCenter
                text: root.mode === "episodes" ? "#" : Model.GLYPH_SEARCH
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
              Text {
                anchors.left: searchGlyph.right
                anchors.leftMargin: Style.spacing.sm
                anchors.right: parent.right
                anchors.rightMargin: Style.spacing.controlPaddingX
                anchors.verticalCenter: parent.verticalCenter
                text: {
                  if (root.mode === "range") return root.rangeText === "" ? "1-12 or 3,5 · Enter saves them" : root.rangeText + "▏"
                  if (root.mode === "episodes") return root.digits !== "" ? root.digits + "▏" : "type an episode number"
                  return root.query === "" ? "search anime" : root.query + "▏"
                }
                readonly property bool hint: (root.mode === "range" && root.rangeText === "") || (root.mode === "episodes" && root.digits === "") || (root.mode !== "range" && root.mode !== "episodes" && root.query === "")
                textFormat: Text.PlainText
                color: hint ? root.dim : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                // A long query keeps its tail in view; a hint keeps its start.
                elide: hint ? Text.ElideRight : Text.ElideLeft
              }
            }
            Text {
              id: busyText
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: root.busy ? "…" : ""
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
            }
          }

          // Notice / error line.
          Text {
            width: parent.width
            visible: text !== ""
            text: root.notice !== "" ? root.notice : root.errorText
            color: root.notice !== "" ? root.accent : Color.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }

          // The list.
          ListView {
            id: list
            width: parent.width
            height: parent.height - y - footer.height - nowPlaying.height - parent.spacing * 2
            clip: true
            model: root.rows.length
            spacing: Style.space(2)
            boundsBehavior: Flickable.StopAtBounds
            // A row is a section header (small, dim, not selectable), the
            // empty hint, or an item: glyph column, title, a second line or
            // a provider chip, and an accent bar when selected.
            delegate: Item {
              required property int index
              readonly property var row: root.rows[index] || ({})
              readonly property bool header: row.kind === "header"
              readonly property bool selected: !header && index === root.cursor
              readonly property bool kept: row.kind === "later" || row.later === true
              readonly property bool chipKind: row.kind === "result" || row.kind === "later"
              width: list.width
              height: header ? root.headerHeight : root.rowHeight
              Rectangle {
                anchors.fill: parent
                radius: Style.cornerRadius
                color: root.selectedBackground
                visible: selected
              }
              Rectangle {
                width: Style.space(3)
                height: parent.height - Style.space(10)
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                radius: width
                color: root.accent
                visible: selected
              }
              MouseArea {
                anchors.fill: parent
                enabled: Model.selectable(row)
                hoverEnabled: true
                onEntered: root.cursor = index
                onClicked: { root.cursor = index; root.activate() }
              }
              Text {
                visible: header
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.rowPaddingX
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Style.space(3)
                text: header ? String(row.title || "").toUpperCase() : ""
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1
              }
              Text {
                id: mark
                visible: !header
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.rowPaddingX
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(18)
                text: Model.rowGlyph(row)
                color: selected || kept ? root.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
              }
              Rectangle {
                id: chip
                visible: !header && chipKind && String(row.subtitle || "") !== ""
                anchors.right: parent.right
                anchors.rightMargin: Style.spacing.rowPaddingX
                anchors.verticalCenter: parent.verticalCenter
                width: chipLabel.implicitWidth + Style.spacing.controlPaddingX
                height: chipLabel.implicitHeight + Style.space(4)
                radius: Style.cornerRadius
                color: selected ? root.background : root.selectedBackground
                Text {
                  id: chipLabel
                  anchors.centerIn: parent
                  text: String(row.subtitle || "")
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
              Column {
                visible: !header
                anchors.left: mark.right
                anchors.leftMargin: Style.spacing.sm
                anchors.right: chip.visible ? chip.left : parent.right
                anchors.rightMargin: Style.spacing.rowPaddingX
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                Text {
                  width: parent.width
                  text: String(row.title || "")
                  textFormat: Text.PlainText
                  color: row.kind === "empty" ? root.dim : (selected ? root.accent : root.foreground)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  visible: text !== ""
                  text: chipKind ? "" : (row.kind === "episode" ? (row.state === "watched" ? "watched" : (row.state === "unfinished" ? "unfinished" : "")) : String(row.subtitle || ""))
                  textFormat: Text.PlainText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
              }
            }
          }

          // Now playing strip.
          Item {
            id: nowPlaying
            width: parent.width
            height: root.playing ? Style.font.body + Style.spacing.controlPaddingY * 2 : 0
            visible: root.playing
            clip: true
            Text {
              anchors.left: parent.left
              anchors.right: controls.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.playing ? Model.nowPlayingLine(root.service.playing, root.service.paused, root.service.position, root.service.duration) : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
            Row {
              id: controls
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.spacing.lg
              Repeater {
                model: [{ glyph: "⏯", act: "pause" }, { glyph: "⏭", act: "next" }, { glyph: "⏹", act: "stop" }]
                delegate: Text {
                  required property var modelData
                  text: modelData.glyph
                  color: hover.containsMouse ? root.accent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  MouseArea {
                    id: hover
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                      if (!root.serviceReady) return
                      if (modelData.act === "pause") root.service.togglePause()
                      else if (modelData.act === "next") root.service.next()
                      else root.service.stop()
                    }
                  }
                }
              }
            }
          }

          // Footer: setting chips and the key hints.
          Column {
            id: footer
            width: parent.width
            spacing: Style.space(3)
            Row {
              spacing: Style.spacing.md
              Repeater {
                model: Model.chips(root.settings).concat(root.serviceReady && root.service.activeDownloads > 0 ? ["↓" + root.service.activeDownloads] : [])
                delegate: Rectangle {
                  required property string modelData
                  width: chipText.implicitWidth + Style.spacing.controlPaddingX
                  height: chipText.implicitHeight + Style.space(4)
                  radius: Style.cornerRadius
                  color: root.selectedBackground
                  Text {
                    id: chipText
                    anchors.centerIn: parent
                    text: modelData
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
            Text {
              width: parent.width
              text: Model.keyHints(root.mode, root.playing)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }
          }
        }
      }
    }
  }

  Component.onCompleted: root.resolveService()
}
