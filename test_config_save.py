import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config_save as cs


class InventoryTests(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "router.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return cs.load_routers(argparse.Namespace(routers_json=str(path)))

    def test_topology_filters_and_credentials(self):
        routers = self.read({
            "devices": [
                {"name": "r1", "type": "junos", "mgmt_ip": "192.0.2.1",
                 "login": "lab/fake/password/"},
                {"name": "JRD Host", "type": "jrd_host"},
                {"name": "IXIA", "type": "ixia_tester"},
            ],
        })
        self.assertEqual(list(routers), ["r1"])
        self.assertEqual(routers["r1"].host, "192.0.2.1")
        self.assertEqual(routers["r1"].user, "lab")
        self.assertEqual(routers["r1"].password, "fake/password/")

    def test_defaults_and_explicit_overrides(self):
        routers = self.read({
            "defaults": {"user": "fallback", "password": "fake-default"},
            "devices": [
                {"name": "r1", "type": "junos", "mgmt_ip": "192.0.2.1"},
                {"name": "r2", "type": "junos", "mgmt_ip": "192.0.2.2",
                 "login": "lab/fake", "user": "override", "password": "",
                 "port": 2222, "tags": ["core"]},
            ],
        })
        self.assertEqual(routers["r1"].user, "fallback")
        self.assertEqual(routers["r1"].password, "fake-default")
        self.assertEqual(routers["r2"].user, "override")
        self.assertIsNone(routers["r2"].password)
        self.assertEqual(routers["r2"].port, 2222)
        self.assertEqual(routers["r2"].tags, ["core"])

    def test_existing_formats(self):
        for data in (
            {"r1": "192.0.2.1"},
            {"defaults": {"user": "lab"}, "routers": {"r1": {"host": "192.0.2.1"}}},
        ):
            with self.subTest(data=data):
                self.assertEqual(self.read(data)["r1"].host, "192.0.2.1")

    def test_invalid_topology_inventory(self):
        device = {"name": "r1", "type": "junos", "mgmt_ip": "192.0.2.1"}
        cases = [
            {"devices": {}}, {"devices": [None]}, {"devices": []},
            {"devices": [device, device]},
            {"devices": [{**device, "login": "not-a-login"}]},
            {"devices": [{**device, "login": "/fake"}]},
            {"devices": [{**device, "login": "lab/"}]},
            {"devices": [{**device, "mgmt_ip": "-option"}]},
            {"devices": [{**device, "tags": "core"}]},
        ]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(cs.Fail):
                self.read(data)

    def test_default_and_explicit_path(self):
        args = argparse.Namespace(routers_json=None)
        with patch.dict(cs.os.environ, {}, clear=True):
            with patch.object(Path, "is_file", return_value=True):
                expected = Path("/home/jnpr/projects/$TOPONAME/router.json") if cs.os.name != "nt" else cs.SCRIPT.with_name("routers.json")
                self.assertEqual(cs.routers_json_path(args), expected)
            with patch.object(Path, "is_file", return_value=False):
                self.assertEqual(cs.routers_json_path(args), cs.SCRIPT.with_name("routers.json"))
            args.routers_json = "custom.json"
            self.assertEqual(cs.routers_json_path(args), Path("custom.json"))
        args.routers_json = None
        with patch.dict(cs.os.environ, {"JUNOS_CS_ROUTERS_JSON": "env.json"}):
            self.assertEqual(cs.routers_json_path(args), Path("env.json"))


class SaveTests(unittest.TestCase):
    def test_strict_host_keys_are_default(self):
        router = cs.Router("r1", "192.0.2.1", "lab", None, 22, [])
        self.assertIn("StrictHostKeyChecking=yes", cs.Ssh()._options(router))
        for command in (
            ["save", "r1"], ["load", "-F", "change.set", "r1"],
            ["confirm", "r1"], ["diff", "baseline", "r1.set"],
        ):
            with self.subTest(command=command):
                args = cs.build_parser().parse_args(command)
                self.assertTrue(args.strict_hostkey)
                self.assertIn("StrictHostKeyChecking=yes", cs.Ssh(args.strict_hostkey)._options(router))

    def test_default_save_writes_three_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Path(folder) / "routers.json"
            inventory.write_text(json.dumps({"r1": "192.0.2.1"}), encoding="utf-8")
            args = cs.build_parser().parse_args([
                "save", "--routers-json", str(inventory),
                "--output-dir", folder, "-i", "baseline", "r1",
            ])
            self.assertEqual(cs.parse_formats(args.format), ["set", "stanza", "json"])
            with patch.object(cs, "fetch", return_value="example configuration\n") as fetch:
                self.assertEqual(cs.cmd_save(args), 0)
            self.assertEqual([call.args[2] for call in fetch.call_args_list], ["set", "stanza", "json"])
            saved = Path(folder) / "config" / "baseline"
            self.assertFalse((Path(folder) / "baseline").exists())
            self.assertEqual({p.suffix for p in saved.iterdir()}, {".set", ".config", ".json"})
            manifest = json.loads((saved / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["mapping"]["r1"]), {"set", "stanza", "json"})
            for entry in manifest["mapping"]["r1"].values():
                self.assertTrue((saved / entry["filename"]).is_file())

    def test_cli_format_override_remains_supported(self):
        args = cs.build_parser().parse_args(["save", "-f", "xml", "r1"])
        self.assertEqual(cs.parse_formats(args.format), ["xml"])


class DashboardTests(unittest.TestCase):
    def setUp(self):
        cs._web_previews.clear()
        cs._web_pending.clear()
        router = cs.Router("r1", "192.0.2.1", "lab", None, 22, [])
        self.inventory = patch.object(cs, "load_routers", return_value={"r1": router})
        self.inventory.start()
        self.addCleanup(self.inventory.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.home = patch.object(cs, "WEB_SAVE_ROOT", Path(self.folder.name))
        self.home.start()
        self.addCleanup(self.home.stop)
        self.process = patch.object(cs.subprocess, "run")
        self.run = self.process.start()
        self.addCleanup(self.process.stop)
        self.run.return_value = cs.subprocess.CompletedProcess([], 0, "configuration check succeeds\n", "")

    def body(self, action, **kwargs):
        return {"action": action, "routers": ["r1"], **kwargs}

    def test_save_automatically_requests_three_formats(self):
        self.assertTrue(cs.web_action(self.body("save", identifier="baseline"))["ok"])
        argv = self.run.call_args.args[0]
        self.assertEqual(argv[argv.index("-f") + 1], "set,stanza,json")
        self.assertEqual(argv[argv.index("--output-dir") + 1], self.folder.name)

    def test_browse_and_read_saved_configuration(self):
        folder = Path(self.folder.name) / "config" / "baseline"
        folder.mkdir(parents=True)
        (folder / "r1.set").write_text("set system services ssh\n", encoding="utf-8")
        (folder / "manifest.json").write_text("{}", encoding="utf-8")
        result = cs.web_action({"action": "browse"})
        self.assertEqual([x["filename"] for x in result["files"]], ["r1.set"])
        result = cs.web_action({"action": "read", "identifier": "baseline", "filename": "r1.set"})
        self.assertEqual(result["content"], "set system services ssh\n")
        for filename in ("../router.json", "manifest.json", "missing.set"):
            with self.assertRaises(cs.Fail):
                cs.web_action({"action": "read", "identifier": "baseline", "filename": filename})
        self.run.assert_not_called()

    def test_current_directory_default(self):
        with patch.dict(cs.os.environ, {}, clear=True):
            self.assertEqual(cs.output_dir(argparse.Namespace(output_dir=None)), Path.cwd())

    def test_preview_commit_and_confirm_are_separate(self):
        result = cs.web_action(self.body("preview", filename="change.set", content="set system services ssh\n"))
        preview = result["preview_token"]
        self.assertNotIn("--commit", self.run.call_args.args[0])
        self.run.return_value = cs.subprocess.CompletedProcess(
            [], 0, "--- r1 (commit confirmed 5) ---\ncommit complete\n", "",
        )
        result = cs.web_action(self.body("commit", token=preview, minutes=5))
        self.assertIn("--commit", self.run.call_args.args[0])
        self.assertNotIn(preview, cs._web_previews)
        confirm = result["confirm_token"]
        self.assertEqual(result["routers"], ["r1"])
        cs.web_action(self.body("confirm", token=confirm))
        self.assertEqual(self.run.call_args.args[0][2], "confirm")
        with self.assertRaises(cs.Fail):
            cs.web_action(self.body("confirm", token=confirm))

    def test_failed_preview_never_authorizes_commit(self):
        self.run.return_value = cs.subprocess.CompletedProcess([], 1, "", "error: syntax error")
        result = cs.web_action(self.body("preview", filename="change.set", content="invalid"))
        self.assertFalse(result["ok"])
        self.assertNotIn("preview_token", result)

    def test_rejects_unsafe_requests(self):
        cases = [
            self.body("save", inventory="/etc/passwd"),
            self.body("save", routers=["unknown"]),
            self.body("save", routers=[]),
            self.body("preview", filename="change.json", content="{}"),
            self.body("commit", token="missing", minutes=5),
            self.body("confirm", token="missing"),
            self.body("preview", filename="change.set", content="set x", mode="override"),
        ]
        for body in cases:
            with self.subTest(action=body["action"]), self.assertRaises(cs.Fail):
                cs.web_action(body)
        self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
