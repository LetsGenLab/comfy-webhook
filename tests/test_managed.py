import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from extension.managed.events import event_from_completion as _event, install_queue_hook as _install, owned_input_files as _owned
from extension.managed.outbox import Outbox as _Outbox

def event_from_completion(prompt_id, status, history):
    return _event(prompt_id, status, history, "sample")

def owned_input_files(prompt_id, graph):
    return _owned(prompt_id, graph, "sample-input")

def install_queue_hook(queue, outbox):
    return _install(queue, outbox, "managed_callback_url", {"https://receiver.example/callback"}, "sample-input", "sample")

class Outbox(_Outbox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, input_prefix="sample-input", output_prefix="sample")

class EventTests(unittest.TestCase):
    def test_final_video_is_not_displaced_by_many_earlier_preview_outputs(self):
        prompt_id = str(uuid.uuid4())
        previews = [{"filename": f"preview-{i}.png", "type": "output"} for i in range(40)]
        video = {"filename": f"sample-{prompt_id}_00001_.mp4", "type": "output"}
        history = {"outputs": {"1": {"images": previews}, "142": {"images": [video]}}}
        event = event_from_completion(prompt_id, SimpleNamespace(status_str="success", messages=[]), history)
        self.assertEqual(len(event["outputs"]), 32)
        self.assertEqual(event["outputs"][0]["filename"], video["filename"])
        self.assertEqual(event["outputs"][0]["nodeId"], "142")

        # Multiple matching videos must reach the receiver so it rejects an
        # ambiguous result rather than persisting and deleting the wrong file.
        second = {"filename": f"sample-{prompt_id}_00002_.mp4", "type": "output"}
        history["outputs"]["142"]["images"].append(second)
        event = event_from_completion(prompt_id, SimpleNamespace(status_str="success", messages=[]), history)
        self.assertEqual([item["filename"] for item in event["outputs"][:2]], [video["filename"], second["filename"]])

    def test_all_graph_completions_are_classified_without_input_leakage(self):
        history = {"outputs": {"12": {"gifs": [{"filename": "result.mp4", "subfolder": "videos",
                    "type": "output", "fullpath": "/private/result.mp4"},
                    {"filename": "../secret", "type": "output"}]}}}
        success = SimpleNamespace(status_str="success", messages=[])
        event = event_from_completion("job-1", success, history)
        self.assertEqual(event["status"], "succeeded")
        self.assertEqual(event["outputs"], [{"nodeId": "12", "group": "gifs", "filename": "result.mp4", "subfolder": "videos", "type": "output"}])
        self.assertNotIn("private", json.dumps(event))
        failed = SimpleNamespace(status_str="error", messages=[("execution_error", {"current_inputs": "private"})])
        self.assertEqual(event_from_completion("job-2", failed, history)["status"], "failed")
        stage_error = SimpleNamespace(status_str="success", messages=[("execution_error", {"node_id": "3", "exception_type": "ValueError"})])
        self.assertEqual(event_from_completion("job-stage", stage_error, history)["status"], "failed")
        self.assertEqual(event_from_completion("job-stage", stage_error, history)["error"]["nodeId"], "3")
        interrupted = SimpleNamespace(status_str="error", messages=[("execution_interrupted", {})])
        self.assertEqual(event_from_completion("job-3", interrupted, history)["status"], "interrupted")
        with self.assertRaises(ValueError):
            event_from_completion("job-4", None, {})

    def test_hook_preserves_queue_completion_and_does_not_depend_on_nodes(self):
        recorded = []
        class Queue:
            mutex = threading.Lock()
            currently_running = {7: (0, "job-7", {}, {"managed_callback_url": "https://receiver.example/callback"})}
            def task_done(self, item_id, history_result, status, process_item=None):
                self.currently_running.pop(item_id)
                return "original result"
        queue = Queue()
        install_queue_hook(queue, SimpleNamespace(add=lambda event, callback, inputs: recorded.append((event, callback))))
        status = SimpleNamespace(status_str="error", messages=[])
        self.assertEqual(queue.task_done(7, {"outputs": {}}, status), "original result")
        self.assertEqual(recorded[0][0]["promptId"], "job-7")
        self.assertEqual(recorded[0][0]["status"], "failed")
        self.assertEqual(recorded[0][1], "https://receiver.example/callback")
        with self.assertRaises(RuntimeError):
            install_queue_hook(queue, SimpleNamespace(add=recorded.append))

    def test_outbox_failure_does_not_break_comfy_queue_completion(self):
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, "job-1")}
            def task_done(self, item_id, history_result, status, process_item=None):
                self.currently_running.pop(item_id)
                return "completed"
        class BrokenOutbox:
            def add(self, event, callback=None, inputs=()): raise OSError("disk full")
        queue = Queue()
        install_queue_hook(queue, BrokenOutbox())
        with self.assertLogs("comfy-webhook-managed", level="ERROR"):
            self.assertEqual(queue.task_done(1, {}, SimpleNamespace(status_str="error", messages=[])), "completed")

    def test_completion_event_is_durable_before_queue_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.sqlite")
            box = Outbox(path, "https://example.com/hook")
            class Queue:
                mutex = threading.Lock()
                currently_running = {1: (0, "job-1")}
                def task_done(self, item_id, history_result, status, process_item=None):
                    with sqlite3.connect(path) as db:
                        self_event = db.execute("SELECT id FROM events").fetchone()
                    if self_event != ("job-1",):
                        raise AssertionError("event was not committed before queue completion")
                    raise RuntimeError("simulated crash during queue completion")
            queue = Queue()
            install_queue_hook(queue, box)
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                queue.task_done(1, {}, SimpleNamespace(status_str="error", messages=[]))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT id FROM events").fetchall(), [("job-1",)])

    def test_untrusted_callback_address_is_not_used(self):
        recorded = []
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, "job-1", {}, {"managed_callback_url": "https://attacker.example/"})}
            def task_done(self, *args, **kwargs): return "completed"
        queue = Queue()
        install_queue_hook(queue, SimpleNamespace(add=lambda event, callback, inputs: recorded.append(callback)))
        queue.task_done(1, {}, SimpleNamespace(status_str="error", messages=[]))
        self.assertEqual(recorded, [None])


class DeliveryTests(unittest.TestCase):
    def test_staged_cleanup_is_scoped_and_refuses_live_or_unacknowledged_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);inputs=root/'inputs';inputs.mkdir()
            box=Outbox(str(root/'events.sqlite'),'https://example.com/hook',input_root=str(inputs))
            prompt_id=str(uuid.uuid4());name=f'sample-input-{prompt_id}-token.png';other='unrelated.png'
            (inputs/name).write_bytes(b'data');(inputs/other).write_bytes(b'keep')
            queue=SimpleNamespace(mutex=threading.Lock(),queue=[(1,prompt_id)],currently_running={})
            self.assertFalse(box.cleanup_staged(prompt_id,[name],queue));self.assertTrue((inputs/name).exists())
            queue.queue=[];queue.currently_running={1:(1,prompt_id)}
            self.assertFalse(box.cleanup_staged(prompt_id,[name],queue));self.assertTrue((inputs/name).exists())
            queue.currently_running={}
            box.add({'version':1,'deliveryId':prompt_id,'promptId':prompt_id,'status':'failed','outputs':[]})
            self.assertFalse(box.cleanup_staged(prompt_id,[name],queue));self.assertTrue((inputs/name).exists())
            with sqlite3.connect(box.path) as db: db.execute('DELETE FROM events WHERE id=?',(prompt_id,))
            with self.assertRaises(ValueError): box.cleanup_staged(prompt_id,['../'+other],queue)
            self.assertTrue(box.cleanup_staged(prompt_id,[name],queue));self.assertFalse((inputs/name).exists())
            self.assertTrue((inputs/other).exists());self.assertTrue(box.cleanup_staged(prompt_id,[name],queue))
            queue.queue=[('malformed',)]
            self.assertFalse(box.cleanup_staged(prompt_id,[name],queue))

    def test_optional_signature_covers_both_completion_and_cleanup_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = 'a' * 32
            box = Outbox(str(Path(tmp) / 'events.sqlite'), 'https://example.com/hook', signing_key=key)
            known = box._request('https://example.com/hook', b'{"status":"succeeded","promptId":"job"}')
            self.assertEqual(known.get_header('X-comfy-webhook-signature'),
                             'sha256=4c535807f214fc6c9c3c9b9f8eed3b8c7ad992badbac8b55d405e6b30cdc07fe')
            prompt_id = str(uuid.uuid4())
            box.add({'version': 1, 'deliveryId': prompt_id, 'promptId': prompt_id, 'status': 'failed', 'outputs': []})
            seen = []
            class Response:
                status = 200
                def __init__(self, status): self.result = status
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def read(self, _limit):
                    return json.dumps({'accepted': True, 'status': self.result, 'cleanupAckRequired': True}).encode()
            class Receiver:
                def open(self, request, timeout):
                    expected = 'sha256=' + hmac.new(key.encode(), request.data, hashlib.sha256).hexdigest()
                    self_signature = request.get_header('X-comfy-webhook-signature')
                    seen.append((json.loads(request.data)['status'], self_signature))
                    if self_signature != expected: raise AssertionError('callback signature mismatch')
                    return Response('failed' if len(seen) == 1 else 'cleaned')
            self.assertTrue(box.deliver_one(now=100, opener=Receiver()))
            self.assertEqual([item[0] for item in seen], ['failed', 'cleaned'])
            self.assertNotEqual(seen[0][1], seen[1][1])
            with self.assertRaisesRegex(ValueError, '32 to 1024'):
                Outbox(str(Path(tmp) / 'bad.sqlite'), 'https://example.com/hook', signing_key='short')

    def test_a_slow_callback_does_not_block_another_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(str(Path(tmp) / "events.sqlite"), "https://example.com/hook")
            first, second = str(uuid.uuid4()), str(uuid.uuid4())
            for prompt_id in (first, second):
                box.add({"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "failed", "outputs": []})
            started, release = threading.Event(), threading.Event()
            received = []
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *_args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"failed"}'
            class Receiver:
                def open(self, request, timeout):
                    prompt_id = json.loads(request.data)["promptId"]
                    received.append(prompt_id)
                    if prompt_id == first:
                        started.set()
                        if not release.wait(5): raise TimeoutError("test callback was not released")
                    return Response()
            worker = threading.Thread(target=lambda: box.deliver_one(now=100, opener=Receiver()))
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                self.assertTrue(box.deliver_one(now=100, opener=Receiver()))
                self.assertEqual(received, [first, second])
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_history_is_cleared_even_if_the_callback_wins_the_queue_completion_race(self):
        prompt_id = str(uuid.uuid4())
        class Queue:
            mutex = threading.Lock()
            currently_running = {1: (0, prompt_id, {}, {"managed_callback_url": "https://receiver.example/callback"})}
            history = set()
            def task_done(self, *_args, **_kwargs): self.history.add(prompt_id)
            def delete_history_item(self, value): self.history.discard(value)
        queue = Queue()
        outbox = SimpleNamespace(add=lambda *_args: True, pending_event=lambda _id: None)
        install_queue_hook(queue, outbox)
        queue.task_done(1, {}, SimpleNamespace(status_str="success", messages=[]))
        self.assertNotIn(prompt_id, queue.history)

    def test_recovered_callback_cleans_only_after_acceptance_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_id = str(uuid.uuid4())
            video = root / f"sample-{prompt_id}_00001_.mp4"
            image = root / f"sample-input-{prompt_id}-image.png"
            other = root / "unrelated.mp4"
            for path in (video, image, other): path.write_bytes(b"fixture")
            path = str(root / "events.sqlite")
            cleared = []
            box = Outbox(path, "https://receiver.example/callback", tmp, tmp, cleared.append)
            event = {"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded",
                     "outputs": [{"nodeId": "142", "group": "images", "filename": video.name, "subfolder": "", "type": "output"}]}
            box.add(event, input_files=[image.name])
            self.assertFalse(box.forget_cleaned(prompt_id))
            self.assertFalse(box.recover_cleanup(prompt_id, {"accepted": True, "status": "succeeded", "cleanup": False}))
            self.assertTrue(video.exists()); self.assertTrue(image.exists())
            self.assertTrue(box.recover_cleanup(prompt_id, {"accepted": True, "status": "succeeded", "cleanup": True}))
            self.assertFalse(video.exists()); self.assertFalse(image.exists()); self.assertTrue(other.exists())
            self.assertEqual(cleared, [prompt_id])
            restored = Outbox(path, "https://receiver.example/callback", tmp, tmp, cleared.append)
            self.assertEqual(restored.pending_event(prompt_id)["status"], "cleaned")
            self.assertTrue(restored.recover_cleanup(prompt_id, {"accepted": True, "status": "duplicate", "cleanup": True}))
            self.assertTrue(restored.forget_cleaned(prompt_id))
            self.assertIsNone(restored.pending_event(prompt_id))

    def test_cleaned_recovery_event_retries_the_regular_callback_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_id = str(uuid.uuid4())
            box = Outbox(str(Path(tmp) / "events.sqlite"), "https://receiver.example/callback", input_root=tmp)
            box.add({"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "failed", "outputs": []})
            self.assertTrue(box.recover_cleanup(prompt_id, {"accepted": True, "status": "failed", "cleanup": False}))
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"cleaned"}'
            class Receiver:
                def open(self, request, timeout):
                    self_test.assertEqual(json.loads(request.data)["status"], "cleaned")
                    return Response()
            self_test = self
            self.assertTrue(box.deliver_one(now=100, opener=Receiver()))
            self.assertIsNone(box.pending_event(prompt_id))

    def test_invalid_success_preserves_outputs_but_allows_managed_cleanup(self):
        for use_recovery in (False, True):
            with self.subTest(recovery=use_recovery), tempfile.TemporaryDirectory() as tmp:
                prompt_id = str(uuid.uuid4())
                video = Path(tmp) / f"sample-{prompt_id}_00001_.mp4"
                image = Path(tmp) / f"sample-input-{prompt_id}-image.png"
                video.write_bytes(b"unrecognized output"); image.write_bytes(b"input")
                cleared = []
                box = Outbox(str(Path(tmp) / "events.sqlite"), "https://receiver.example/callback", tmp, tmp, cleared.append)
                box.add({"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded", "outputs": []}, input_files=[image.name])
                acknowledgement = {"accepted": True, "status": "failed", "cleanup": False, "reason": "output_invalid", "cleanupAckRequired": True}
                if use_recovery:
                    acknowledgement["status"] = "duplicate"
                    self.assertTrue(box.recover_cleanup(prompt_id, acknowledgement))
                    self.assertEqual(box.pending_event(prompt_id)["status"], "cleaned")
                else:
                    class Response:
                        status = 200
                        def __init__(self, value): self.value = value
                        def __enter__(self): return self
                        def __exit__(self, *args): pass
                        def read(self, _limit): return json.dumps(self.value).encode()
                    class Receiver:
                        def open(self, request, timeout):
                            return Response({"accepted": True, "status": "cleaned"} if json.loads(request.data)["status"] == "cleaned" else acknowledgement)
                    self.assertTrue(box.deliver_one(now=100, opener=Receiver()))
                    self.assertIsNone(box.pending_event(prompt_id))
                self.assertTrue(video.exists(), "invalid outputs must remain for review")
                self.assertFalse(image.exists(), "acknowledged failed jobs release their inputs")
                self.assertEqual(cleared, [], "failed jobs retain history")

    def test_managed_conflicting_success_never_reports_unconfirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_id = str(uuid.uuid4())
            output = Path(tmp) / f"sample-{prompt_id}_00001_.mp4"
            output.write_bytes(b"video")
            box = Outbox(str(Path(tmp) / "outbox.sqlite"), "https://callback.example", tmp, tmp)
            box.add({"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded",
                     "outputs": [{"filename": output.name, "subfolder": "", "type": "output"}]})
            calls = []
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self, size): return json.dumps({"accepted": True, "status": "duplicate", "cleanup": False,
                                                         "cleanupAckRequired": True}).encode()
            class Receiver:
                def open(self, request, timeout):
                    calls.append(json.loads(request.data))
                    return Response()
            self.assertFalse(box.deliver_one(now=100, opener=Receiver()))
            self.assertEqual([call["status"] for call in calls], ["succeeded"])
            self.assertTrue(output.exists())
            with sqlite3.connect(box.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (1,))

    def test_managed_cleanup_ack_is_retried_after_files_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root, output_root = root / "input", root / "output"
            input_root.mkdir(); output_root.mkdir()
            prompt_id = str(uuid.uuid4())
            input_name = f"sample-input-{prompt_id}-image.png"
            (input_root / input_name).write_bytes(b"image")
            video = output_root / f"sample-{prompt_id}_00001_.mp4"
            video.write_bytes(b"video")
            path = str(root / "events.sqlite")
            cleared = []
            box = Outbox(path, "https://receiver.example/callback", str(output_root), str(input_root), cleared.append)
            event = {"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded",
                     "outputs": [{"nodeId": "142", "group": "images", "filename": video.name, "type": "output"}]}
            box.add(event, input_files=[input_name])
            self.assertEqual(box.pending_event(prompt_id), event)
            self.assertIsNone(box.pending_event("../not-a-prompt"))
            calls = []
            class Response:
                def __init__(self, status, payload): self.status, self.payload = status, payload
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return json.dumps(self.payload).encode()
            class Receiver:
                def open(self, request, timeout):
                    value = json.loads(request.data); calls.append(value)
                    if value["status"] == "cleaned":
                        self_test.assertFalse(video.exists())
                        self_test.assertFalse((input_root / input_name).exists())
                        if sum(call["status"] == "cleaned" for call in calls) == 1:
                            return Response(503, {})
                        return Response(200, {"accepted": True, "status": "cleaned"})
                    return Response(200, {"accepted": True, "status": "succeeded", "cleanup": True, "cleanupAckRequired": True})
            self_test = self
            self.assertFalse(box.deliver_one(now=100, opener=Receiver()))
            self.assertEqual(cleared, [], "unacknowledged cleanup must retain history")
            with sqlite3.connect(path) as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (1,))
            self.assertTrue(box.deliver_one(now=102, opener=Receiver()))
            self.assertEqual(cleared, [prompt_id])
            self.assertIsNone(box.pending_event(prompt_id))
            self.assertEqual([call["status"] for call in calls], ["succeeded", "cleaned", "succeeded", "cleaned"])
            self.assertNotIn("outputs", calls[-1])
            with sqlite3.connect(path) as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (0,))

    def test_own_inputs_follow_callback_ack_and_survive_cleanup_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_root, output_root = root / "input", root / "output"
            input_root.mkdir(); output_root.mkdir()
            prompt_id = str(uuid.uuid4())
            inputs = [f"sample-input-{prompt_id}-{i}.png" for i in range(9)]
            for name in inputs: (input_root / name).write_bytes(b"image")
            other_id = str(uuid.uuid4())
            other = f"sample-input-{other_id}-other.png"
            (input_root / other).write_bytes(b"other")
            graph = {str(i): {"class_type": "LoadImage", "inputs": {"image": name}} for i, name in enumerate(inputs + [other, "../user.png"])}
            self.assertEqual(owned_input_files(prompt_id, graph), sorted(inputs))
            video = output_root / f"sample-{prompt_id}_00001_.mp4"
            video.write_bytes(b"video")
            path = str(root / "events.sqlite")
            callback = "https://receiver.example/callback"
            box = Outbox(path, "", str(output_root), str(input_root))
            class Queue:
                mutex = threading.Lock()
                currently_running = {1: (0, prompt_id, graph, {"managed_callback_url": callback})}
                def task_done(self, item_id, history_result, status, process_item=None):
                    self.currently_running.pop(item_id)
            queue = Queue()
            install_queue_hook(queue, box)
            queue.task_done(1, {"outputs": {"142": {"images": [{"filename": video.name, "type": "output"}]}}},
                            SimpleNamespace(status_str="success", messages=[]))
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"succeeded","cleanup":true}'
            class Delivered:
                def open(self, request, timeout):
                    if b"input_files" in request.data: raise AssertionError("local cleanup metadata leaked")
                    return Response()
            real_unlink = __import__("os").unlink
            def fail_input(name, **kwargs):
                if name == inputs[0]: raise PermissionError("temporarily unavailable")
                return real_unlink(name, **kwargs)
            with patch("extension.managed.outbox.os.unlink", side_effect=fail_input):
                self.assertFalse(box.deliver_one(now=100, opener=Delivered()))
            self.assertFalse(video.exists())
            self.assertTrue((input_root / inputs[0]).exists())
            restored = Outbox(path, "", str(output_root), str(input_root))
            self.assertTrue(restored.deliver_one(now=102, opener=Delivered()))
            self.assertTrue(all(not (input_root / name).exists() for name in inputs))
            self.assertTrue((input_root / other).exists())
            with sqlite3.connect(path) as db: self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (0,))

    def test_ignored_callback_is_retried_after_restart_before_failed_job_cleans_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_id = str(uuid.uuid4())
            name = f"sample-input-{prompt_id}-frame.png"
            (root / name).write_bytes(b"image")
            box = Outbox(str(root / "events.sqlite"), "https://example.com/hook", input_root=tmp)
            event = {"deliveryId": prompt_id, "promptId": prompt_id, "status": "failed", "outputs": []}
            class Receiver:
                def __init__(self, status): self.status = status
                def open(self, request, timeout):
                    status = self.status
                    class Response:
                        status = 200
                        def __enter__(self): return self
                        def __exit__(self, *args): pass
                        def read(self, _limit): return json.dumps({"accepted": True, "status": status}).encode()
                    return Response()
            box.add(event, input_files=[name])
            self.assertFalse(box.deliver_one(now=100, opener=Receiver("ignored")))
            self.assertTrue((root / name).exists())
            with sqlite3.connect(box.path) as db:
                self.assertEqual(db.execute("SELECT attempts, COUNT(*) FROM events").fetchone(), (1, 1))
            box = Outbox(str(root / "events.sqlite"), "https://example.com/hook", input_root=tmp)
            self.assertTrue(box.deliver_one(now=102, opener=Receiver("failed")))
            self.assertFalse((root / name).exists())
            with sqlite3.connect(box.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (0,))

    def test_old_outbox_rows_are_delivered_and_unsafe_input_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = str(root / "events.sqlite")
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, body BLOB NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, callback TEXT)")
                db.execute("INSERT INTO events(id,body,callback) VALUES(?,?,?)", ("old-job", b'{"deliveryId":"old-job","status":"failed"}', "https://example.com/hook"))
            Path(path).chmod(0o600)
            box = Outbox(path, "", input_root=tmp)
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"failed"}'
            class Delivered:
                def open(self, request, timeout): return Response()
            self.assertTrue(box.deliver_one(now=100, opener=Delivered()))
            prompt_id = str(uuid.uuid4())
            for name in ("../user.png", f"sample-input-{uuid.uuid4()}-x.png", f"sample-input-{prompt_id}-x.png/../../user.png"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    box.cleanup_inputs(prompt_id, [name])

    def test_ignored_event_does_not_block_other_ready_callbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = Outbox(str(Path(tmp) / "events.sqlite"), "https://example.com/hook")
            box.add({"deliveryId": "first", "status": "failed"})
            box.add({"deliveryId": "second", "status": "failed"})
            class Receiver:
                def open(self, request, timeout):
                    status = "ignored" if json.loads(request.data)["deliveryId"] == "first" else "failed"
                    class Response:
                        status = 200
                        def __enter__(self): return self
                        def __exit__(self, *args): pass
                        def read(self, _limit): return json.dumps({"accepted": True, "status": status}).encode()
                    return Response()
            self.assertFalse(box.deliver_one(now=100, opener=Receiver()))
            self.assertTrue(box.deliver_one(now=100, opener=Receiver()))
            with sqlite3.connect(box.path) as db:
                self.assertEqual(db.execute("SELECT id FROM events").fetchall(), [("first",)])

    def test_retryable_callback_keeps_output_until_worker_acknowledges_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_id = str(uuid.uuid4())
            output = Path(tmp) / f"sample-{prompt_id}_00001_.mp4"
            output.write_bytes(b"video")
            path = str(Path(tmp) / "events.sqlite")
            box = Outbox(path, "", tmp)
            callback = "https://receiver.example/callback"
            box.add({"version": 1, "deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded", "outputs": [
                {"nodeId": "142", "group": "images", "filename": output.name, "subfolder": "", "type": "output"}]}, callback)
            class Response:
                def __init__(self, status, body): self.status, self.body = status, body
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return self.body
            class Receiver:
                def __init__(self, status, body): self.status, self.body = status, body
                def open(self, request, timeout):
                    self_url = request.full_url
                    if self_url != callback: raise AssertionError("callback destination changed")
                    return Response(self.status, self.body)
            self.assertFalse(box.deliver_one(now=100, opener=Receiver(503, b'{}')))
            self.assertTrue(output.exists())
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT attempts FROM events WHERE id=?", (prompt_id,)).fetchone(), (1,))
            # Simulate an extension restart before the receiver confirms persistence.
            restored = Outbox(path, "", tmp)
            self.assertFalse(restored.deliver_one(now=102, opener=Receiver(200, b'{"accepted":true,"status":"ignored"}')))
            self.assertTrue(output.exists())
            self.assertFalse(restored.deliver_one(now=104, opener=Receiver(200, b'{"accepted":true,"status":"failed"}')))
            self.assertTrue(output.exists())
            self.assertFalse(restored.deliver_one(now=108, opener=Receiver(200, b'{"accepted":true,"status":"succeeded","cleanup":false}')))
            self.assertTrue(output.exists())
            self.assertTrue(restored.deliver_one(now=116, opener=Receiver(200, b'{"accepted":true,"status":"succeeded","cleanup":true}')))
            self.assertFalse(output.exists())
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (0,))

    def test_fasth3_cleanup_deletes_only_acknowledged_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_id = str(uuid.uuid4())
            target = Path(tmp) / f"sample-{prompt_id}_00001_.mp4"
            target.write_bytes(b"video")
            other = Path(tmp) / "user.mp4"
            other.write_bytes(b"user")
            box = Outbox(str(Path(tmp) / "events.sqlite"), "", tmp)
            box.cleanup_output({"promptId": prompt_id, "outputs": [{"nodeId": "142", "group": "images", "filename": target.name, "subfolder": "", "type": "output"}]})
            self.assertFalse(target.exists())
            self.assertTrue(other.exists())

    def test_invalid_video_result_cleans_inputs_but_preserves_unrecognized_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_id = str(uuid.uuid4())
            input_name = f"sample-input-{prompt_id}-frame.png"
            (root / input_name).write_bytes(b"image")
            unrecognized = root / "other-video.mp4"
            unrecognized.write_bytes(b"video")
            box = Outbox(str(root / "events.sqlite"), "https://example.com/hook", tmp, tmp)
            box.add({"deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded",
                     "outputs": [{"nodeId": "142", "group": "images", "filename": unrecognized.name, "type": "output"}]},
                    input_files=[input_name])
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"failed","cleanup":false,"reason":"output_invalid"}'
            class Delivered:
                def open(self, request, timeout): return Response()
            self.assertTrue(box.deliver_one(now=100, opener=Delivered()))
            self.assertFalse((root / input_name).exists())
            self.assertTrue(unrecognized.exists())
            with sqlite3.connect(box.path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone(), (0,))

    def test_only_acknowledged_job_output_is_deleted_and_cleanup_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt_id = str(uuid.uuid4())
            output = Path(tmp) / f"sample-{prompt_id}_00001-audio.mp4"
            other = Path(tmp) / "user-video.mp4"
            output.write_bytes(b"video")
            other.write_bytes(b"user")
            box = Outbox(str(Path(tmp) / "events.sqlite"), "https://example.com/hook", tmp)
            box.add({"deliveryId": prompt_id, "promptId": prompt_id, "status": "succeeded", "outputs": [
                {"nodeId": "24", "group": "gifs", "filename": output.name, "subfolder": "", "type": "output"}]})
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"succeeded","cleanup":true}'
            class Delivered:
                def open(self, request, timeout): return Response()
            with patch("extension.managed.outbox.os.unlink", side_effect=PermissionError("denied")):
                self.assertFalse(box.deliver_one(now=100, opener=Delivered()))
            self.assertTrue(output.exists())
            self.assertTrue(box.deliver_one(now=102, opener=Delivered()))
            self.assertFalse(output.exists())
            self.assertTrue(other.exists())

    def test_per_job_callback_survives_outbox_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.sqlite")
            box = Outbox(path, "https://example.com/default")
            target = "https://receiver.example/callback"
            box.add({"deliveryId": "job-1", "status": "failed"}, target)
            requests = []
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"failed"}'
            class Delivered:
                def open(self, request, timeout): requests.append(request.full_url); return Response()
            self.assertTrue(Outbox(path, "https://example.com/default").deliver_one(now=100, opener=Delivered()))
            self.assertEqual(requests, [target])

    def test_outbox_persists_and_retries_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.sqlite")
            box = Outbox(path, "https://example.com/hook")
            event = {"deliveryId": "job-1", "status": "failed"}
            box.add(event)
            box.add(event)
            class Failed:
                def open(self, request, timeout):
                    raise OSError("offline")
            self.assertFalse(box.deliver_one(now=100, opener=Failed()))
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            sent = []
            class Response:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self, _limit): return b'{"accepted":true,"status":"failed"}'
            class Delivered:
                def open(self, request, timeout):
                    sent.append(request)
                    return Response()
            restored = Outbox(path, "https://example.com/hook")
            self.assertTrue(restored.deliver_one(now=102, opener=Delivered()))
            req = sent[0]
            self.assertIsNone(req.get_header("X-modal-callback-signature"))
            self.assertEqual(req.get_header("User-agent"), "ComfyUI-Webhook-Managed/1.0")
            self.assertEqual(json.loads(req.data), event)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_rejects_insecure_or_missing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            for url in ("http://example.com/", "https://user:pass@example.com/"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    Outbox(str(Path(tmp) / "events.sqlite"), url)
