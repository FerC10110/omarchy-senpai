import QtQuick
import Quickshell.Io

// One bin/senpai call at a time, with a policy for what a second start()
// means while one is still running: "replace" abandons the running call
// (its result is dropped: a newer search supersedes an older one) and
// "queue" runs the new call after the current one (user actions keep their
// order). Every run has a deadline so a dead network never freezes the UI.
Item {
  id: runner

  property string program: ""
  property int timeoutMs: 45000
  property string policy: "replace"      // replace | queue
  property bool pending: false

  signal finished(int code, string out, string err)

  property int _gen: 0
  property var _run: null
  property var _queued: null

  function start(args) {
    if (pending) {
      if (policy === "queue") { _queued = args; return true }
      _abandon()
    }
    _launch(args)
    return true
  }

  function _launch(args) {
    _gen += 1
    pending = true
    var run = runComponent.createObject(runner, { gen: _gen, command: ["python3", program].concat(args) })
    _run = run
    run.running = true
    watchdog.restart()
  }

  function _abandon() {
    var run = _run
    _run = null
    pending = false
    watchdog.stop()
    settle.stop()
    if (run && !run.done) { run.done = true; run.running = false; run.destroy() }
  }

  function _settle(run) {
    if (run.done) return
    if (run !== _run) { run.done = true; run.destroy(); return }
    if (!run.timedOut && !(run.exited && run.outDone && run.errDone)) return
    run.done = true
    _run = null
    pending = false
    watchdog.stop()
    settle.stop()
    var code = run.timedOut ? 1 : run.code
    var out = run.timedOut ? "" : run.out
    var err = run.timedOut ? "senpai: took too long and was stopped" : run.err
    if (run.timedOut) run.running = false
    run.destroy()
    finished(code, out, err)
    if (_queued !== null) { var next = _queued; _queued = null; _launch(next) }
  }

  Component {
    id: runComponent
    Process {
      id: run
      property int gen: 0
      property bool done: false
      property bool timedOut: false
      property string out: ""
      property string err: ""
      property int code: 0
      property bool exited: false
      property bool outDone: false
      property bool errDone: false
      stdout: StdioCollector {
        waitForEnd: true
        onStreamFinished: { run.out = String(text || ""); run.outDone = true; runner._settle(run) }
      }
      stderr: StdioCollector {
        waitForEnd: true
        onStreamFinished: { run.err = String(text || ""); run.errDone = true; runner._settle(run) }
      }
      onExited: function(exitCode) {
        run.code = exitCode
        run.exited = true
        runner._settle(run)
        if (!run.done) settle.restart()
      }
    }
  }

  // The streams normally report right after exit; if they never do, deliver anyway.
  Timer {
    id: settle
    interval: 250
    onTriggered: { var run = runner._run; if (run && run.exited) { run.outDone = true; run.errDone = true; runner._settle(run) } }
  }
  Timer {
    id: watchdog
    interval: runner.timeoutMs
    onTriggered: { var run = runner._run; if (run) { run.timedOut = true; runner._settle(run) } }
  }
}
