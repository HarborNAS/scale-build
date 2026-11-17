import importlib.util
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from truenas_install import harbor_demo_nginx as compat


IDENTITY_HEADERS = (
    "X-Harbor-Principal-Id", "X-Harbor-Principal-Source", "X-Harbor-Principal-Roles",
    "X-Harbor-Camera-Scope", "X-Harbor-Workspace-Id", "X-Harbor-User-Id", "X-Harbor-Edge-Assertion",
)


def native_location(shape):
    if "beacon" in shape:
        target = "http://127.0.0.1:8787/api/beacon" + ("/" if shape.endswith("/") else "")
    else:
        target = "http://127.0.0.1:8787"
    headers = "".join(f'    proxy_set_header {name} "";\n' for name in IDENTITY_HEADERS)
    return f'''location {shape} {{
    proxy_pass {target};
    proxy_set_header Authorization "";
    proxy_set_header X-HarborOS-Auth-Token $http_x_harboros_auth_token;
{headers}    limit_req zone=harbor_assistant burst=40 nodelay;
    limit_req_status 429;
    client_max_body_size 32m;
    proxy_read_timeout 200s;
    proxy_send_timeout 200s;
}}
'''


UNRELATED = '''location /websocket {
    proxy_pass http://127.0.0.1:6000/websocket;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
location /api/v2.0 { proxy_pass http://127.0.0.1:6000; }
location /rwd/ { proxy_pass http://127.0.0.1:1700/; }
'''
NATIVE = "server {\n" + "".join(map(native_location, compat.SHAPES)) + UNRELATED + '''location @index {
    root /usr/share/truenas/webui;
}
location = /ui/sw.js {
    alias /usr/share/truenas/webui/sw.js;
}
}
'''
SERVICE = """[Unit]
Description=Restore photo demo nginx overlay after middleware regeneration
After=nginx.service harbor-gate-webui-edge.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 """ + compat.RELEASE_ROOT + "/deploy_photo_demo.py reconcile-nginx --release " + compat.RELEASE + "\n"


class HarborDemoNginxTests(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            lock = patch.object(compat, "_configuration_lock", side_effect=lambda _: nullcontext())
            lock.start()
            self.addCleanup(lock.stop)

    def test_both_beacon_aliases_keep_uri_and_use_nas_authenticated_edge(self):
        rendered = compat.render_nginx(NATIVE)
        locations = {m["shape"].strip(): m["body"] for m in compat.LOCATION.finditer(rendered)}
        for shape in compat.SHAPES[:2]:
            body = locations[shape]
            self.assertIn("proxy_pass http://127.0.0.1:4176;", body)
            self.assertIn("proxy_set_header Authorization $http_authorization;", body)
            for header in IDENTITY_HEADERS:
                self.assertIn(f'proxy_set_header {header} "";', body)
            for directive in ("limit_req zone=harbor_assistant burst=40 nodelay;", "limit_req_status 429;",
                              "client_max_body_size 32m;", "proxy_read_timeout 200s;", "proxy_send_timeout 200s;"):
                self.assertIn(directive, body)
        self.assertIn(UNRELATED, rendered)
        self.assertIn("root " + compat.RELEASE_ROOT + "/ui;", rendered)
        self.assertIn("alias " + compat.RELEASE_ROOT + "/ui/sw.js;", rendered)

    def test_every_external_legacy_gate_path_is_denied_without_changing_loopback(self):
        rendered = compat.render_nginx(NATIVE)
        locations = {m["shape"].strip(): m["body"] for m in compat.LOCATION.finditer(rendered)}
        for shape in compat.SHAPES[2:]:
            self.assertIn("return 404;", locations[shape])
            self.assertNotIn("proxy_pass", locations[shape])
        self.assertNotIn(":8787", rendered)
        self.assertNotIn(":4174", rendered)
        # No wildcard legacy Gate prefix can reach setup, turns, notifications or Beacon.
        self.assertIn("^~ /api/harbor-gate/", locations)

    def test_idempotent_for_http_and_https_servers(self):
        native = NATIVE + NATIVE.replace("server {", "server {\nlisten 443 ssl;", 1)
        rendered = compat.render_nginx(native)
        self.assertEqual(compat.render_nginx(rendered), rendered)
        self.assertEqual(rendered.count("proxy_pass http://127.0.0.1:4176;"), 4)

    def test_rejects_forged_or_missing_identity_cleaning(self):
        for header in IDENTITY_HEADERS:
            for replacement in (f"proxy_set_header {header} $http_forged;", "# removed"):
                with self.subTest(header=header, replacement=replacement), self.assertRaises(ValueError):
                    compat.render_nginx(NATIVE.replace(f'proxy_set_header {header} "";', replacement, 1))
        with self.assertRaises(ValueError):
            compat.render_nginx(NATIVE.replace('proxy_set_header Authorization "";',
                                             "proxy_set_header Authorization $http_authorization;", 1))

    def test_rejects_unknown_private_proxies_and_route_shapes(self):
        for port in (4174, 4176, 8787):
            for path in ("/api/beacon", "/api/harbor-assistant", "/unexpected"):
                for separator in (" ", "\n"):
                    with self.subTest(port=port, path=path, separator=separator), self.assertRaises(ValueError):
                        compat.render_nginx(NATIVE + f"location {path} {{{separator}proxy_pass http://127.0.0.1:{port};{separator}}}\n")
        for old, new in (("location = /api/harbor-beacon", "location /api/harbor-beacon"),
                         ("location ^~ /api/harbor-gate/", "location /api/harbor-gate/")):
            with self.subTest(new=new), self.assertRaises(ValueError):
                compat.render_nginx(NATIVE.replace(old, new))
        with self.assertRaises(ValueError):
            compat.render_nginx(NATIVE.replace(native_location(compat.SHAPES[0]), ""))
        with self.assertRaises(ValueError):
            compat.render_nginx("upstream old_gate { server 127.0.0.1:8787; }\n" + NATIVE +
                                "location /api/beacon { proxy_pass http://old_gate; }\n")

    def test_migration_produces_only_reviewed_files_and_standalone_helper(self):
        files = compat.migration_files(SERVICE)
        self.assertEqual(set(files), {compat.SERVICE_PATH, compat.HELPER_PATH, compat.NGINX_DROPIN_PATH})
        service = files[compat.SERVICE_PATH].decode()
        self.assertIn(compat.MODE, service)
        self.assertIn("After=nginx.service harbor-gate-webui-edge.service", service)
        self.assertIn("ExecStart=/usr/bin/python3 /" + compat.HELPER_PATH + " --release " + compat.RELEASE, service)
        self.assertNotIn("deploy_photo_demo.py", service)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "standalone.py"
            path.write_bytes(files[compat.HELPER_PATH])
            spec = importlib.util.spec_from_file_location("standalone_compat", path)
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            self.assertEqual(helper.render_nginx(NATIVE), compat.render_nginx(NATIVE))

    def test_native_start_and_reload_run_compat_synchronously_first(self):
        dropin = compat.migration_files(SERVICE)[compat.NGINX_DROPIN_PATH].decode()
        command = "/usr/bin/python3 /" + compat.HELPER_PATH + " --release " + compat.RELEASE + " --render-only"
        self.assertEqual([line for line in dropin.splitlines() if line.startswith("Exec")], [
            "ExecStartPre=", "ExecStartPre=" + command,
            "ExecStartPre=/usr/sbin/nginx -t -q -g 'daemon on; master_process on;'",
            "ExecReload=", "ExecReload=" + command,
            "ExecReload=/usr/sbin/nginx -g 'daemon on; master_process on;' -s reload",
        ])

    def test_render_only_hook_never_recursively_reloads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nginx.conf"
            path.write_text(NATIVE)
            commands = []
            compat.reconcile_nginx(path, lambda command, check: commands.append(command[:2]), render_only=True)
            compat.reconcile_nginx(path, lambda command, check: commands.append(command[:2]), render_only=True)
            self.assertEqual(commands, [["nginx", "-t"]])

    def test_migration_rejects_unreviewed_release_or_service(self):
        for service in (SERVICE.replace("-r4", "-r5"), SERVICE + "ExecStart=/bin/true\n",
                        SERVICE.replace("/usr/bin/python3", "/usr/bin/env python3")):
            with self.subTest(service=service), self.assertRaises(ValueError):
                compat.migration_files(service)

    def test_reconcile_validates_before_replace_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nginx.conf"
            path.write_text(NATIVE)
            commands = []

            def run(command, check):
                self.assertTrue(check)
                commands.append(command[:2])
                if command[0] == "nginx":
                    self.assertEqual(path.read_text(), NATIVE)
                    self.assertEqual(Path(command[-1]).read_text(), compat.render_nginx(NATIVE))
                else:
                    self.assertEqual(path.read_text(), compat.render_nginx(NATIVE))

            compat.reconcile_nginx(path, run)
            compat.reconcile_nginx(path, run)
            self.assertEqual(commands, [["nginx", "-t"], ["systemctl", "reload"]])

    def test_failed_validation_or_concurrent_regeneration_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nginx.conf"
            path.write_text(NATIVE)

            def fail(command, check):
                raise subprocess.CalledProcessError(1, command)

            with self.assertRaises(subprocess.CalledProcessError):
                compat.reconcile_nginx(path, fail)
            self.assertEqual(path.read_text(), NATIVE)

            def regenerate(command, check):
                path.write_text("new middleware configuration")

            with self.assertRaisesRegex(ValueError, "changed"):
                compat.reconcile_nginx(path, regenerate)
            self.assertEqual(path.read_text(), "new middleware configuration")
            self.assertFalse(list(Path(directory).glob("nginx.harbor-demo-*.conf")))

    @unittest.skipUnless(os.name == "posix", "Linux runtime lock")
    def test_lock_serializes_workers_and_is_released_before_recursive_reload(self):
        import multiprocessing
        import threading

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nginx.conf"
            path.write_text(NATIVE)
            entered = multiprocessing.Event()
            release = multiprocessing.Event()

            def holder():
                with compat._configuration_lock(path):
                    entered.set()
                    release.wait(10)

            worker = multiprocessing.get_context("fork").Process(target=holder)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                validated = threading.Event()

                def run(command, check):
                    if command[0] == "nginx":
                        validated.set()
                    else:
                        # A reload hook can take the same lock: the watcher has
                        # released it before calling systemctl.
                        compat.reconcile_nginx(path, render_only=True)

                outcome = []

                def reconcile():
                    try:
                        compat.reconcile_nginx(path, run)
                        outcome.append("success")
                    except Exception as error:
                        outcome.append(error)

                thread = threading.Thread(target=reconcile, daemon=True)
                thread.start()
                self.assertFalse(validated.wait(0.1))
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive(), "recursive reload deadlocked")
                self.assertEqual(outcome, ["success"])
            finally:
                release.set()
                worker.join(5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
            self.assertEqual(worker.exitcode, 0)
