import QtQuick
import qs.Commons
import qs.Ui
import "SenpaiModel.js" as Model

// BarWidget.qml -- the bar entry of io.github.ferc10110.senpai. Owns no
// state: everything comes from Service.qml via bar.shell.serviceFor.
// Left click opens the finder, right click pauses/resumes, middle click stops.
BarWidget {
  id: root
  moduleName: Model.PLUGIN_ID

  property var service: null
  readonly property bool serviceReady: service !== null
  readonly property bool playing: serviceReady && service.isPlaying === true
  readonly property bool paused: playing && service.paused === true
  readonly property int downloads: serviceReady ? service.activeDownloads : 0
  readonly property bool showTitle: Model.boolSetting(setting("showTitleInBar", true))
  readonly property int labelMaxWidth: Style.space(Model.intSetting(setting("barLabelMaxWidth", 180), 180, 60, 600))
  readonly property string labelText: {
    var text = root.playing && root.showTitle ? Model.barLabel(root.service.playing) : ""
    if (root.downloads > 0) text = (text ? text + "  " : "") + "↓" + root.downloads
    return text
  }
  readonly property bool showLabel: !root.vertical && root.labelText !== ""
  readonly property color barFg: bar ? bar.barForeground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string glyph: root.paused ? Model.GLYPH_PAUSED : (root.playing ? Model.GLYPH_PLAYING : Model.GLYPH_IDLE)
  readonly property color glyphColor: root.playing ? root.barFg : Util.alpha(root.barFg, 0.6)
  readonly property string tooltipText: Model.tooltip({
    playing: root.playing ? root.service.playing : null,
    paused: root.paused,
    position: root.playing ? root.service.position : 0,
    duration: root.playing ? root.service.duration : 0,
    subtitle: root.playing ? root.service.subtitle : null,
    downloads: root.downloads
  })

  implicitWidth: icon.width + labelHolder.width
  implicitHeight: root.barSize

  function resolveService() {
    if (root.service) return
    if (root.bar && root.bar.shell && typeof root.bar.shell.serviceFor === "function")
      root.service = root.bar.shell.serviceFor(root.moduleName)
    if (!root.service && !resolveTimer.running) resolveTimer.start()
  }

  function openFinder() {
    if (root.bar && root.bar.shell && typeof root.bar.shell.toggle === "function")
      root.bar.shell.toggle(root.moduleName, "{}")
  }

  function handlePress(button) {
    if (root.bar) root.bar.hideTooltip(root)
    root.resolveService()
    if (button === Qt.RightButton) { if (root.service) root.service.togglePause() }
    else if (button === Qt.MiddleButton) { if (root.service) root.service.stop() }
    else root.openFinder()
  }

  Component.onCompleted: resolveService()

  Timer {
    id: resolveTimer
    property int tries: 0
    interval: 500
    repeat: true
    onTriggered: { tries++; root.resolveService(); if (root.service || tries >= 20) resolveTimer.stop() }
  }

  BarIconButton {
    id: icon
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    bar: root.bar
    text: root.glyph
    foreground: root.glyphColor
    useActiveColor: false
    interactive: false
    pressable: false
  }

  Item {
    id: labelHolder
    anchors.left: icon.right
    anchors.verticalCenter: parent.verticalCenter
    height: root.barSize
    width: root.showLabel ? Math.min(label.implicitWidth, root.labelMaxWidth) + Style.spaceReal(8.5) : 0
    clip: true
    Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }

    Text {
      id: label
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      width: Math.min(implicitWidth, root.labelMaxWidth)
      textFormat: Text.PlainText
      text: root.labelText
      visible: root.showLabel
      color: root.barFg
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
      renderType: Text.NativeRendering
      elide: Text.ElideRight
      verticalAlignment: Text.AlignVCenter
    }
  }

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onEntered: { root.resolveService(); if (root.bar) root.bar.showTooltip(root, root.tooltipText) }
    onExited: if (root.bar) root.bar.hideTooltip(root)
    onClicked: function(mouse) { root.handlePress(mouse.button) }
  }
}
