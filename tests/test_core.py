"""Core tests. Run from the repo root:  python -m unittest discover tests

Offline tests always run. Online tests hit CurseForge and install real addons into a
throwaway temp folder; set WOWAM_OFFLINE=1 to skip them.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "addon_manager.pyw"
_loader = importlib.machinery.SourceFileLoader("addon_manager", str(SRC))
am = importlib.util.module_from_spec(importlib.util.spec_from_loader("addon_manager", _loader))
_loader.exec_module(am)

OFFLINE = os.environ.get("WOWAM_OFFLINE") == "1"
QUESTIE = 334372
DBM = 3358


def fake_wow():
    """Temp 'World of Warcraft' tree with a Forever client 1.60.1 and an empty AddOns folder."""
    work = Path(tempfile.mkdtemp(prefix="wowam_test_"))
    wow = work / "World of Warcraft"
    addons = wow / am.ADDONS_SUBPATH
    addons.mkdir(parents=True)
    (wow / ".build.info").write_text(
        "Branch!STRING:0|Active!DEC:1|Version!STRING:0|Product!STRING:0\n"
        "us|1|1.60.1.70862|wowf\n", encoding="utf-8")
    (wow / "_forever_" / ".flavor.info").write_text("Product Flavor!STRING:0\nwowf\n", encoding="utf-8")
    return work, addons


def add_folder(addons, name, toc):
    (addons / name).mkdir()
    (addons / name / f"{name}.toc").write_text(toc, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.work, self.addons = fake_wow()
        am.REGISTER_PATH = self.work / "addons.json"
        self.reg = am.load_register()
        self.reg["addons_dir"] = str(self.addons)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)


class Offline(Base):
    def test_game_version_prefix(self):
        self.assertEqual(am.game_version_prefix(self.addons), "1.60")

    def test_compatibility(self):
        self.assertTrue(am.is_compatible({"gameVersions": ["5.5.4", "1.60.1"]}, "1.60"))
        self.assertFalse(am.is_compatible({"gameVersions": ["1.15.9", "11.2.0"]}, "1.60"))

    def test_parse_link(self):
        self.assertEqual(am.parse_link("https://www.curseforge.com/wow/addons/questie/download/9502146"),
                         ("questie", 9502146))
        self.assertEqual(am.parse_link("https://www.curseforge.com/wow/addons/deadly-boss-mods"),
                         ("deadly-boss-mods", None))
        with self.assertRaises(ValueError):
            am.parse_link("https://example.com/wow/addons")

    def test_toc_title_cleanup(self):
        add_folder(self.addons, "Questie", "## Title: Questie|cFF00FF00 v12.6.4|r\n## Version: 12.6.4\n")
        self.assertEqual(am.read_toc(self.addons / "Questie")["Title"], "Questie")

    def test_scan_adopts_and_lists_untracked(self):
        add_folder(self.addons, "Questie", f"## Title: Questie\n## Version: 12.6.4\n## X-Curse-Project-ID: {QUESTIE}\n")
        add_folder(self.addons, "Handmade", "## Title: Mine\n")
        rep = am.scan(self.reg)
        self.assertEqual(rep["adopted"], ["Questie"])
        self.assertEqual(rep["untracked"], ["Handmade"])
        self.assertEqual(self.reg["addons"][str(QUESTIE)]["version"], "12.6.4")

    def test_scan_detects_deletions(self):
        add_folder(self.addons, "A-Core", "## X-Curse-Project-ID: 1\n")
        add_folder(self.addons, "A-Extra", "## X-Curse-Project-ID: 1\n")
        am.scan(self.reg)
        self.assertEqual(self.reg["addons"]["1"]["folders"], ["A-Core", "A-Extra"])
        shutil.rmtree(self.addons / "A-Extra")
        am.scan(self.reg)
        self.assertEqual(self.reg["addons"]["1"]["missing"], ["A-Extra"])
        shutil.rmtree(self.addons / "A-Core")
        rep = am.scan(self.reg)
        self.assertNotIn("1", self.reg["addons"])
        self.assertTrue(rep["removed"])

    def test_update_due(self):
        e = {"file_id": 1, "latest": {"file_id": 2, "version": "v2", "file_name": "x-v2.zip"}}
        self.assertTrue(am.update_due(e))
        e["file_id"] = 2
        self.assertFalse(am.update_due(e))
        adopted = {"file_id": None, "version": "12.6.4", "latest": {"file_id": 9, "version": "v12.6.4", "file_name": ""}}
        self.assertFalse(am.update_due(adopted))

    def test_register_roundtrip(self):
        self.reg["addons"]["5"] = {"name": "X", "folders": ["X"]}
        am.save_register(self.reg)
        self.assertEqual(json.loads(am.REGISTER_PATH.read_text(encoding="utf-8"))["addons"]["5"]["name"], "X")

    def test_url_allowlist(self):
        for bad in ("https://example.com/", "http://www.curseforge.com/api/v1/mods/1/files",
                    "https://www.curseforge.com.attacker.net/"):
            with self.assertRaises(am.NetError):
                am.http_open(bad)
        with self.assertRaises(am.NetError):
            am._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.example/x.zip")

    def test_zip_slip_rejected(self):
        for bad in ("../evil.lua", "C:/evil.lua", "/abs/evil.lua", "Root.toc"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("Good/Good.toc", "x")
                z.writestr(bad, "x")
            with self.assertRaises(ValueError, msg=bad):
                am.safe_top_folders(zipfile.ZipFile(buf))


@unittest.skipIf(OFFLINE, "WOWAM_OFFLINE=1")
class Online(Base):
    def test_install_update_uninstall(self):
        prefix = am.game_version_prefix(self.addons)

        latest = am.latest_file(QUESTIE, prefix)
        self.assertIsNotNone(latest)
        e = am.install_file(self.reg, QUESTIE, am.get_file(QUESTIE, latest["id"]), "questie")
        self.assertTrue((self.addons / "Questie").is_dir())
        self.assertEqual(e["name"], "Questie")

        (self.addons / "Questie" / "STALE.txt").write_text("old")
        self.reg["addons"][str(QUESTIE)]["file_id"] = 1  # pretend it's outdated
        am.check_updates(self.reg, prefix)
        self.assertTrue(am.update_due(self.reg["addons"][str(QUESTIE)]))
        am.install_file(self.reg, QUESTIE, am.get_file(QUESTIE, latest["id"]), "questie")
        self.assertFalse(am.update_due(self.reg["addons"][str(QUESTIE)]))
        self.assertFalse((self.addons / "Questie" / "STALE.txt").exists())

        am.uninstall(self.reg, QUESTIE)
        self.assertFalse((self.addons / "Questie").exists())
        self.assertNotIn(str(QUESTIE), am.load_register()["addons"])

    def test_multi_folder_addon(self):
        prefix = am.game_version_prefix(self.addons)
        e = am.install_file(self.reg, DBM, am.latest_file(DBM, prefix), "deadly-boss-mods")
        self.assertGreater(len(e["folders"]), 1)
        self.assertTrue(all((self.addons / f).is_dir() for f in e["folders"]))

    def test_wrong_project_id_rejected(self):
        with self.assertRaises(am.NetError):
            am.get_file(1, 9502146)


if __name__ == "__main__":
    unittest.main()
