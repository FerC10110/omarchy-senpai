import QtQuick

// Replaced by Task 9.
Item {
  property bool opened: false
  function open(payloadJson) { opened = true }
  function close() { opened = false }
}
