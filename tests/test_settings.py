import json
import os
import tempfile
import unittest
from pathlib import Path

from extension.settings import load


class SettingsTests(unittest.TestCase):
    def test_private_settings_survive_restart_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"comfy-webhook"/"settings.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"COMFY_WEBHOOK_API_TOKEN":"private", "COMFY_WEBHOOK_MANAGED_ENABLED":"1"}))
            path.chmod(0o600)
            values=load(root,{"COMFY_WEBHOOK_MANAGED_ENABLED":"0"})
            self.assertEqual(values["COMFY_WEBHOOK_API_TOKEN"],"private")
            self.assertEqual(values["COMFY_WEBHOOK_MANAGED_ENABLED"],"0")
            self.assertEqual(load(root,{})["COMFY_WEBHOOK_MANAGED_ENABLED"],"1")

    def test_rejects_unrelated_values_and_insecure_file(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/"comfy-webhook"/"settings.json";path.parent.mkdir()
            path.write_text('{"OTHER_SECRET":"value"}');path.chmod(0o600)
            with self.assertRaises(ValueError):load(root,{})
            if os.name=="posix":
                path.write_text('{}');path.chmod(0o644)
                with self.assertRaises(ValueError):load(root,{})
