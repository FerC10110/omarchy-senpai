import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def font_covers(codepoint: int) -> bool:
    """True when an installed Nerd Font has the glyph (fc-query charset ranges)."""
    done = subprocess.run(["fc-list", ":fontformat=TrueType", "file"], capture_output=True, text=True)
    fonts = [line.split(":")[0].strip() for line in done.stdout.splitlines() if "Nerd" in line or "NF" in line]
    for font in fonts[:6]:
        charset = subprocess.run(["fc-query", "--format=%{charset}\n", font], capture_output=True, text=True).stdout
        for chunk in charset.split():
            lo, _, hi = chunk.partition("-")
            lo_v, hi_v = int(lo, 16), int(hi or lo, 16)
            if lo_v <= codepoint <= hi_v:
                return True
    return not fonts  # no Nerd Font installed: nothing to check against


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))

    def test_identity_and_kinds(self):
        m = self.manifest
        self.assertEqual(m["schemaVersion"], 1)
        self.assertEqual(m["id"], "io.github.ferc10110.senpai")
        self.assertRegex(m["id"], r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
        self.assertEqual(sorted(m["kinds"]), ["bar-widget", "overlay", "service"])
        self.assertTrue(m["keepLoaded"])
        for kind, key in (("bar-widget", "barWidget"), ("overlay", "overlay"), ("service", "service")):
            self.assertIn(key, m["entryPoints"], kind)
            self.assertTrue((ROOT / m["entryPoints"][key]).is_file(), m["entryPoints"][key])

    def test_defaults_and_schema_agree_with_the_model(self):
        widget = self.manifest["barWidget"]
        model = (ROOT / "SenpaiModel.js").read_text(encoding="utf-8")
        defaults_block = model[model.index("var DEFAULTS = {"):model.index("}", model.index("var DEFAULTS = {"))]
        model_keys = set(re.findall(r"^\s+(\w+):", defaults_block, re.MULTILINE))
        self.assertEqual(set(widget["defaults"]), model_keys)
        self.assertEqual([field["key"] for field in widget["schema"]], list(widget["defaults"]))
        for field in widget["schema"]:
            self.assertIn(field["type"], {"string", "boolean", "integer"})
            self.assertTrue(field["label"])
        self.assertIn(widget["defaultSection"], {"left", "center", "right"})
        self.assertFalse(widget["allowMultiple"])

    def test_glyphs_exist_in_the_installed_nerd_font(self):
        model = (ROOT / "SenpaiModel.js").read_text(encoding="utf-8")
        for name in ("GLYPH_IDLE", "GLYPH_PLAYING", "GLYPH_PAUSED"):
            match = re.search(name + r' = "([^"]+)"', model)
            self.assertIsNotNone(match, name)
            literal = match.group(1)
            if literal.startswith("\\u"):  # "\uf008" or "\u{f008}"
                codepoint = int(literal[2:].strip("{}"), 16)
            else:  # the glyph itself
                self.assertEqual(len(literal), 1, name)
                codepoint = ord(literal)
            self.assertTrue(font_covers(codepoint), f"{name} U+{codepoint:04x} missing from the Nerd Font")

    def test_no_symlinks_and_no_literal_colors_in_qml(self):
        for path in ROOT.rglob("*"):
            if ".git" in path.parts:
                continue
            self.assertFalse(path.is_symlink(), path)
        for qml in ROOT.glob("*.qml"):
            text = qml.read_text(encoding="utf-8")
            self.assertNotRegex(text, r'"#[0-9a-fA-F]{3,8}"', f"literal color in {qml.name}")


if __name__ == "__main__":
    unittest.main()
