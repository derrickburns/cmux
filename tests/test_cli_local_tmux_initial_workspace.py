"""Run the real CLI against an owned stand-in socket and tmux executable.

CMUX_CLI_PATH selects the built CLI; no app or actual tmux server is opened.
The stand-in records spawn requests and never executes their commands.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import uuid


class LocalTmuxInitialWorkspaceTest(unittest.TestCase):
    def setUp(self):
        explicit = os.environ.get("CMUX_CLI_PATH")
        lane_cli = os.environ.get("CMUX_CLI_BIN")
        if explicit and lane_cli and Path(explicit).resolve() != Path(lane_cli).resolve():
            raise RuntimeError("CMUX_CLI_PATH and the maintained lane's CMUX_CLI_BIN disagree")
        self.cli = explicit or lane_cli
        if not self.cli or not Path(self.cli).is_file():
            raise RuntimeError("Set CMUX_CLI_PATH to the source-bound built CLI")
        self.temp = tempfile.TemporaryDirectory(prefix="cmux-local-initial-", dir="/private/tmp")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.window = str(uuid.uuid4())
        self.workspace = str(uuid.uuid4())
        self.surface = str(uuid.uuid4())
        self.logical = str(uuid.uuid4())
        self.server_id = str(uuid.uuid4())
        self.password = "synthetic-fixture." + uuid.uuid4().hex
        self.requests = []
        self.failures = []
        self.drop_create_reply = False
        self.existing_workspace = False
        self.closed = threading.Event()
        self.socket_path = str(self.root / "app.sock")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.socket_path)
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.addCleanup(self.stop_server)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()
        self.state = self.root / "profile"
        self.state.mkdir(mode=0o700)
        registry = self.state / "sessions.json"
        registry.write_text(json.dumps({"version": 1, "sessions": [{
            "id": self.logical, "name": "original", "cwd": str(self.root),
            "tmuxBinding": {"sessionID": "$31", "serverID": self.server_id, "sessionCreated": 31},
            "socketPath": str(self.state / "server.sock"), "createdAt": 1.0, "updatedAt": 1.0,
        }]}))
        registry.chmod(0o600)
        self.fake_tmux = self.root / "fake-tmux"
        self.fake_tmux.write_text("#!/bin/sh\ncase \"$*\" in\n"
            " *display-message*) printf 'original\\t$31\\t" + self.server_id + "\\t31\\n' ;;\n"
            " *list-clients*) exit 0 ;;\n"
            " *) exit 0 ;;\nesac\n")
        self.fake_tmux.chmod(0o755)

    def stop_server(self):
        self.closed.set()
        self.listener.close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive(), "owned fixture socket did not stop")

    def serve(self):
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    stream = connection.makefile("rwb")
                    with stream:
                        for raw in stream:
                            line = raw.decode().strip()
                            if line.startswith("auth "):
                                if line != "auth " + self.password:
                                    self.failures.append("wrong synthetic authentication")
                                    return
                                stream.write(b"OK\n")
                                stream.flush()
                                continue
                            request = json.loads(line)
                            self.requests.append(request)
                            method = request["method"]
                            params = request.get("params", {})
                            result = self.response(method, params)
                            if method == "workspace.create" and self.drop_create_reply:
                                return  # Accepted in the stand-in; deliberately lose only its reply.
                            stream.write((json.dumps({"id": request["id"], "ok": True, "result": result}) + "\n").encode())
                            stream.flush()
                except (OSError, ValueError, KeyError) as error:
                    if not self.closed.is_set():
                        self.failures.append(type(error).__name__)

    def response(self, method, params):
        if method == "window.list":
            return {"windows": [{"id": self.window}]}
        if method == "workspace.current":
            return {"workspace_id": self.workspace} if self.existing_workspace else {}
        if method == "workspace.list":
            return {"workspaces": [{"id": self.workspace, "title": "Owned fixture", "current_directory": str(self.root)}]}
        if method == "surface.list":
            return {"surfaces": []}
        if method in {"workspace.create", "surface.create", "surface.respawn"}:
            return {"workspace_id": self.workspace, "surface_id": self.surface, "window_id": self.window}
        self.failures.append("unexpected method: " + method)
        return {}

    def attach(self, *options):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("CMUX_")}
        environment.update(CMUX_CLI_SENTRY_DISABLED="1", CMUX_SOCKET_PATH=self.socket_path,
            CMUX_SOCKET_PASSWORD=self.password, CMUX_LOCAL_TMUX_BIN=str(self.fake_tmux),
            CMUX_LOCAL_TMUX_STATE_DIR=str(self.state))
        return subprocess.run([self.cli, "--socket", self.socket_path, "--json", "--id-format", "uuids",
            "local-tmux", "attach", "--id", self.logical, "--window", self.window, *options],
            capture_output=True, text=True, env=environment, timeout=12)

    def methods(self):
        return [request["method"] for request in self.requests]

    def test_initial_workspace_receives_the_owned_command_without_respawning_a_surface(self):
        result = self.attach()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.failures, [])
        created = [request for request in self.requests if request["method"] == "workspace.create"]
        self.assertEqual(len(created), 1)
        self.assertNotIn("surface.respawn", self.methods(), "a newly visible surface may already contain input")
        command = created[0]["params"].get("initial_command")
        self.assertIsInstance(command, str, "the initial terminal must own the attach command at creation")
        self.assertIn("@cmux_local_server_id", command)
        self.assertIn(self.server_id, command)
        self.assertEqual(created[0]["params"].get("tmux_start_command"), command)
        reply = json.loads(result.stdout)
        self.assertEqual(uuid.UUID(reply["workspace_id"]), uuid.UUID(self.workspace))
        self.assertEqual(uuid.UUID(reply["surface_id"]), uuid.UUID(self.surface))
        self.assertEqual(uuid.UUID(reply["session_id"]), uuid.UUID(self.logical))
        self.assertEqual(reply["tmux_start_command"], command)

    def test_lost_initial_create_reply_is_not_reissued_or_followed_by_respawn(self):
        self.drop_create_reply = True
        result = self.attach()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.methods().count("workspace.create"), 1)
        self.assertNotIn("surface.respawn", self.methods())
        self.assertNotIn("surface.create", self.methods())
        registry = json.loads((self.state / "sessions.json").read_text())
        self.assertIsNone(registry["sessions"][0].get("workspaceID"))
        self.assertIsNone(registry["sessions"][0].get("surfaceID"))

    def test_existing_workspace_creates_a_separate_surface_without_replacing_input(self):
        self.existing_workspace = True
        result = self.attach("--workspace", self.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.failures, [])
        self.assertEqual(self.methods().count("surface.create"), 1)
        self.assertNotIn("workspace.create", self.methods())
        self.assertNotIn("surface.respawn", self.methods())

    def test_new_workspace_selection_ignores_existing_surfaces_and_returns_the_owner_command(self):
        self.existing_workspace = True
        result = self.attach("--new-workspace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.failures, [])
        self.assertEqual(self.methods().count("workspace.create"), 1)
        self.assertNotIn("workspace.current", self.methods())
        self.assertNotIn("surface.list", self.methods())
        self.assertNotIn("surface.create", self.methods())
        self.assertNotIn("surface.respawn", self.methods())
        request = next(item for item in self.requests if item["method"] == "workspace.create")
        self.assertEqual(request["params"]["initial_command"], request["params"]["tmux_start_command"])
        self.assertEqual(json.loads(result.stdout)["tmux_start_command"], request["params"]["tmux_start_command"])

    def test_new_workspace_rejects_conflicting_targets_before_dispatch(self):
        for options in (("--workspace", self.workspace), ("--surface", self.surface),
                        ("--headless",), ("--detached",), ("--new-client",)):
            with self.subTest(options=options):
                result = self.attach("--new-workspace", *options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--new-workspace", result.stderr)
                self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
