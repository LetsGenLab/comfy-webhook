import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from extension.events import install_queue_hook, workflow_settings
from extension.outbox import Outbox


class Response:
    status = 204
    def __enter__(self): return self
    def __exit__(self, *_args): pass


class Receiver:
    def __init__(self, fail=False): self.fail, self.calls = fail, 0
    def open(self, _request, timeout):
        self.calls += 1
        if self.fail: raise OSError("receiver unavailable")
        return Response()


class AutoCleanTests(unittest.TestCase):
    def test_queue_uses_switch_and_preserves_cached_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            new=Path(tmp)/"new.mp4";new.write_bytes(b"new")
            cached=Path(tmp)/"cached.mp4";cached.write_bytes(b"cached")
            graph={"7":{"class_type":"ComfyWebhook", "inputs":{"url":"https://example.com", "auto_clean_once_done":True}}}
            class Queue:
                mutex=threading.Lock()
                currently_running={1:(0,"job",graph,{})}
                def task_done(self,*args,**kwargs):return "done"
            box=Outbox(str(Path(tmp)/"events.sqlite"),output_root=tmp);queue=Queue();install_queue_hook(queue,box)
            history={"outputs":{"1":{"images":[{"filename":"new.mp4","type":"output"}]},
                                "2":{"images":[{"filename":"cached.mp4","type":"output"}]}}}
            status=SimpleNamespace(status_str="success",messages=[("execution_cached",{"nodes":["2"]})])
            self.assertEqual(queue.task_done(1,history,status),"done")
            self.assertTrue(new.exists());self.assertTrue(box.deliver_one(now=100,opener=Receiver()))
            self.assertFalse(new.exists());self.assertTrue(cached.exists())

    def test_node_setting_is_optional_and_must_be_literal(self):
        self.assertEqual(workflow_settings({"1":{"class_type":"ComfyWebhook","inputs":{"url":"https://example.com"}}}),
                         ("https://example.com", False))
        graph={"1":{"class_type":"ComfyWebhook","inputs":{"url":"https://example.com", "auto_clean_once_done":True}}}
        self.assertEqual(workflow_settings(graph), ("https://example.com", True))
        graph["1"]["inputs"]["auto_clean_once_done"]=["other",0]
        with self.assertRaises(ValueError): workflow_settings(graph)

    def test_cleans_only_uncached_unchanged_outputs_after_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"outputs";root.mkdir();(root/"nested").mkdir()
            own=root/"nested"/"own.mp4";own.write_bytes(b"own")
            cached=root/"cached.mp4";cached.write_bytes(b"cached")
            changed=root/"changed.mp4";changed.write_bytes(b"old")
            unrelated=root/"unrelated.mp4";unrelated.write_bytes(b"unrelated")
            link=root/"link.mp4";link.symlink_to(unrelated)
            outputs=[{"nodeId":"1","filename":"own.mp4","subfolder":"nested","type":"output"},
                     {"nodeId":"2","filename":"cached.mp4","type":"output"},
                     {"nodeId":"3","filename":"changed.mp4","type":"output"},
                     {"nodeId":"4","filename":"link.mp4","type":"output"},
                     {"nodeId":"5","filename":"../unrelated.mp4","type":"output"}]
            path=str(Path(tmp)/"events.sqlite");event={"deliveryId":"job", "status":"succeeded", "outputs":outputs}
            box=Outbox(path,output_root=str(root));box.add(event,"https://example.com",auto_clean=True,cached_nodes={"2"})
            changed.write_bytes(b"newer")
            self.assertFalse(box.deliver_one(now=100,opener=Receiver(fail=True)))
            self.assertTrue(own.exists())
            restored=Outbox(path,output_root=str(root));self.assertTrue(restored.deliver_one(now=101,opener=Receiver()))
            self.assertFalse(own.exists());self.assertTrue(cached.exists());self.assertEqual(changed.read_bytes(),b"newer")
            self.assertTrue(link.is_symlink());self.assertEqual(unrelated.read_bytes(),b"unrelated")

    def test_disabled_switch_preserves_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"result.mp4";p.write_bytes(b"data")
            box=Outbox(str(Path(tmp)/"events.sqlite"),output_root=tmp)
            box.add({"deliveryId":"job","outputs":[{"nodeId":"1","filename":p.name,"type":"output"}]},
                    "https://example.com")
            self.assertTrue(box.deliver_one(now=1,opener=Receiver()));self.assertTrue(p.exists())

    def test_portable_cleanup_rejects_symlinked_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"outputs";root.mkdir();outside=Path(tmp)/"outside";outside.mkdir()
            (outside/"other.mp4").write_bytes(b"outside");(root/"link").symlink_to(outside, target_is_directory=True)
            own=root/"own.mp4";own.write_bytes(b"own")
            event={"deliveryId":"job","outputs":[{"nodeId":"1","filename":"own.mp4","type":"output"},
                    {"nodeId":"2","filename":"other.mp4","subfolder":"link","type":"output"}]}
            with patch("extension.cleanup._HAS_DIR_FD",False):
                box=Outbox(str(Path(tmp)/"events.sqlite"),output_root=str(root));box.add(event,"https://example.com",auto_clean=True)
                self.assertTrue(box.deliver_one(now=1,opener=Receiver()))
            self.assertFalse(own.exists());self.assertEqual((outside/"other.mp4").read_bytes(),b"outside")

    def test_cleanup_failure_retries_without_duplicate_delivery_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"result.mp4";p.write_bytes(b"data");path=str(Path(tmp)/"events.sqlite")
            box=Outbox(path,output_root=tmp);box.add({"deliveryId":"job","outputs":[{"nodeId":"1","filename":p.name,"type":"output"}]},
                                                     "https://example.com",auto_clean=True)
            receiver=Receiver()
            with patch("extension.outbox.remove_unchanged",side_effect=OSError("busy")):
                self.assertFalse(box.deliver_one(now=100,opener=receiver))
            self.assertEqual(receiver.calls,1);self.assertTrue(p.exists())
            restored=Outbox(path,output_root=tmp);self.assertTrue(restored.deliver_one(now=101,opener=receiver))
            self.assertEqual(receiver.calls,1);self.assertFalse(p.exists())
