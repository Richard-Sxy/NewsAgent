"""Standard-library tests; no GPU/model download or user process termination."""
import hashlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import assets
import manage


class AssetsTest(unittest.TestCase):
    def fixture(self, root):
        payload = b"test weights"
        (root / "weights.bin").write_bytes(payload)
        manifest = {"repo_id": "tencent/Hunyuan-7B-Instruct", "revision": assets.REVISION,
                    "files": [{"path": "weights.bin", "bytes": len(payload),
                               "sha256": hashlib.sha256(payload).hexdigest()}]}
        path = root / "download-manifest.json"
        path.write_text(json.dumps(manifest))
        return assets.checksum(path)

    def test_same_size_weight_corruption_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin = self.fixture(root)
            with patch.object(assets, "MANIFEST_SHA256", pin):
                assets.verify(root)
                (root / "weights.bin").write_bytes(b"fake weights")
                with self.assertRaises(ValueError):
                    assets.verify(root)

    def test_changed_manifest_and_symbolic_weights_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pin = self.fixture(root)
            with patch.object(assets, "MANIFEST_SHA256", pin):
                (root / "weights.bin").rename(root / "actual.bin")
                (root / "weights.bin").symlink_to(root / "actual.bin")
                with self.assertRaises(ValueError):
                    assets.verify(root)
                (root / "download-manifest.json").write_text("{}")
                with self.assertRaises(ValueError):
                    assets.manifest(root)

    def test_import_is_repeatable_and_does_not_replace_different_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source", root / "target"
            source.mkdir()
            pin = self.fixture(source)
            with patch.object(assets, "MANIFEST_SHA256", pin), patch.object(assets, "MODEL_DIR", target):
                # verify() has a constant default path; check the target explicitly here.
                original_verify = assets.verify
                with patch.object(assets, "verify", side_effect=lambda: original_verify(target)):
                    assets.import_assets(source)
                    assets.import_assets(source)
                    (target / "weights.bin").write_bytes(b"other")
                    with self.assertRaises(FileExistsError):
                        assets.import_assets(source)


class ProcessControlTest(unittest.TestCase):
    def receipt(self):
        return {"pid": 123, "start_ticks": "100",
                "argv": [str(manage.PYTHON), "-u", str(manage.SERVER), "--port", "8008"], "port": 8008}

    def test_pid_reuse_or_different_command_is_not_owned(self):
        receipt = self.receipt()
        current = {"uid": os.getuid(), "state": "S", "start_ticks": "100", "argv": receipt["argv"]}
        with patch.object(manage, "process_identity", return_value=current):
            self.assertTrue(manage.owned(receipt))
            current["start_ticks"] = "101"
            self.assertFalse(manage.owned(receipt))
            current["start_ticks"] = "100"
            current["argv"] = ["unrelated", "service"]
            self.assertFalse(manage.owned(receipt))

    def test_stop_does_not_signal_a_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt_path = root / "receipt.json"
            receipt_path.write_text(json.dumps(self.receipt()))
            with (patch.object(manage, "STATE", root), patch.object(manage, "RECEIPT", receipt_path),
                  patch.object(manage, "owned", return_value=False), patch.object(os, "kill") as kill):
                self.assertEqual(manage.stop()["status"], "not_running")
                kill.assert_not_called()

    def test_busy_port_never_launches_a_second_service(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as listener:
            root = Path(tmp)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with (patch.object(manage, "STATE", root), patch.object(manage, "RECEIPT", root / "receipt.json"),
                  patch.object(manage.subprocess, "Popen") as spawn):
                with self.assertRaises(OSError):
                    manage.start(port, 0)
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
