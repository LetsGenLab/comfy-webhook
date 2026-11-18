import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


INSTALLER = Path(__file__).resolve().parents[1] / "install.py"
TOKEN = "COMFY_WEBHOOK_API_TOKEN"
PASSWORD = "COMFY_WEBHOOK_BASIC_PASSWORD"


class InstallerTests(unittest.TestCase):
    def test_fresh_install_and_password_rotation_preserve_token_and_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()

            def git(*args):
                return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()

            git("init", "-q")
            (source / "__init__.py").touch()
            git("add", "__init__.py")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Plugin")
            revision = git("rev-parse", "HEAD")

            comfy = root / "comfy"
            (comfy / "custom_nodes").mkdir(parents=True)
            (comfy / "main.py").touch()
            (comfy / "folder_paths.py").touch()
            user_dir = root / "persistent-user"
            env = {key: value for key, value in os.environ.items() if not key.startswith("COMFY_WEBHOOK_")}
            env.update({
                "COMFYUI_DIR": str(comfy),
                "COMFYUI_USER_DIR": str(user_dir),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{source.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://github.com/LetsGenLab/comfy-webhook.git",
                "COMFY_WEBHOOK_MANAGED_ENABLED": "1",
                "COMFY_WEBHOOK_MANAGED_CALLBACKS": "https://example.invalid/callback",
            })

            def run(**settings):
                return subprocess.run([sys.executable, str(INSTALLER), revision],
                                      env={**env, **settings}, capture_output=True, text=True, timeout=20)

            failed = run(**{TOKEN: "weak"})
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((comfy / "custom_nodes" / "comfy-webhook").exists())
            initial = run(**{PASSWORD: "comfy"})
            self.assertEqual(initial.returncode, 0, initial.stderr)
            private_dir = user_dir / "comfy-webhook"
            path = private_dir / "settings.json"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            settings = json.loads(path.read_text())
            self.assertGreaterEqual(len(settings[TOKEN]), 32)
            self.assertEqual(settings[PASSWORD], "comfy")
            self.assertNotIn(settings[TOKEN], initial.stdout + initial.stderr)

            outbox = private_dir / "managed-outbox.sqlite"
            outbox.write_text("existing outbox")
            del env["COMFY_WEBHOOK_MANAGED_CALLBACKS"]
            updated = run(**{PASSWORD: "replacement password"})
            self.assertEqual(updated.returncode, 0, updated.stderr)
            self.assertEqual(json.loads(path.read_text()), {**settings, PASSWORD: "replacement password"})
            self.assertEqual(outbox.read_text(), "existing outbox")
