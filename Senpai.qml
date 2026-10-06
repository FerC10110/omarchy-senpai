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
                            anime: root.anime, busy: root.busy, error: root.errorText })
  }
  function pressKey(name) {
    var n = String(name || "")
    if (n === "up") root.moveCursor(-1)
    else if (n === "down") root.moveCursor(1)
    else if (n === "enter") root.activate()
    else if (n === "escape") root.back()
    else if (n === "ctrl+d") root.downloadHighlighted()
    else return "unknown"
    return "ok"
  }

  // ---- modes
  function showHome() {
    root.mode = "home"
    root.rows = Model.homeRows(root.serviceReady ? root.service.home : null)
    root.cursor = 0
    root.scrollToCursor()
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
      root.rows = Model.resultRows(payload)
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
      root.scrollToCursor()
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
    root.cursor = Model.clampIndex(root.cursor + delta, root.rows.length)
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
      if (row.kind === "recent") { root.openEpisodes(row.provider, row.id, row.animeTitle, "home"); return }
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
    if (alt && event.key === Qt.Key_P) { root.cycleSetting("provider", Model.PROVIDERS); return true }
    if (alt && event.key === Qt.Key_Q) { root.cycleSetting("quality", Model.QUALITIES); return true }
    if (alt && event.key === Qt.Key_A) { if (root.serviceReady) root.service.setSetting("dub", !root.settings.dub); return true }
    if (alt && event.key === Qt.Key_N) { if (root.serviceReady) root.service.setSetting("autoNext", !root.settings.autoNext); return true }
    if (ctrl && event.key === Qt.Key_Space) { if (root.serviceReady) root.service.togglePause(); return true }
    if (ctrl && event.key === Qt.Key_N) { if (root.serviceReady) root.service.next(); return true }
    if (ctrl && event.key === Qt.Key_S) { if (root.serviceReady) root.service.stop(); return true }
    if (ctrl && shift && event.key === Qt.Key_D) { root.startRange(); return true }
    if (ctrl && event.key === Qt.Key_D) { root.downloadHighlighted(); return true }
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

          // Header: prompt + query (or range), busy marker.
          Item {
            width: parent.width
            height: Style.font.heading + Style.spacing.controlPaddingY * 2
            Text {
              id: prompt
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              text: root.mode === "range" ? "Download episodes" : (root.mode === "episodes" && root.anime ? String(root.anime.title) : "Senpai")
              color: root.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
              font.bold: true
              elide: Text.ElideRight
              width: Math.min(implicitWidth, parent.width * 0.5)
            }
            Text {
              anchors.left: prompt.right
              anchors.leftMargin: Style.spacing.lg
              anchors.right: busyText.left
              anchors.rightMargin: Style.spacing.md
              anchors.verticalCenter: parent.verticalCenter
              text: {
                if (root.mode === "range") return root.rangeText === "" ? "1-12, 3,5" : root.rangeText + "▏"
                if (root.mode === "episodes") return root.digits !== "" ? "jump " + root.digits : "type a number · Enter plays · Ctrl+D downloads · Ctrl+Shift+D range"
                return root.query === "" ? "type to search" : root.query + "▏"
              }
              color: (root.mode === "range" && root.rangeText === "") || (root.mode === "episodes" && root.digits === "") || (root.mode !== "range" && root.mode !== "episodes" && root.query === "") ? root.dim : root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.heading
              elide: Text.ElideLeft
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
            delegate: Rectangle {
              required property int index
              readonly property var row: root.rows[index] || ({})
              readonly property bool selected: index === root.cursor
              width: list.width
              height: root.rowHeight
              radius: Style.cornerRadius
              color: selected ? root.selectedBackground : "transparent"
              MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                onEntered: root.cursor = index
                onClicked: { root.cursor = index; root.activate() }
              }
              Text {
                id: mark
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.rowPaddingX
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(18)
                text: row.kind === "episode" ? String(row.mark || "") : (row.kind === "continue" ? "▶" : "")
                color: selected ? root.accent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
              }
              Column {
                anchors.left: mark.right
                anchors.leftMargin: Style.spacing.sm
                anchors.right: parent.right
                anchors.rightMargin: Style.spacing.rowPaddingX
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                Text {
                  width: parent.width
                  text: String(row.title || "")
                  color: selected ? root.accent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  visible: text !== ""
                  text: row.kind === "episode" ? (row.state === "watched" ? "watched" : (row.state === "unfinished" ? "unfinished" : "")) : String(row.subtitle || "")
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
              text: "Enter play · Esc back · Ctrl+D download · Ctrl+Shift+D range · Alt+P provider · Alt+A audio · Alt+Q quality · Alt+N auto-next · Ctrl+Space pause · Ctrl+N next · Ctrl+S stop"
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
