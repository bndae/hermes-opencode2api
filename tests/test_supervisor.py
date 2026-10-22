import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("opencode2api_plugin_supervisor", PLUGIN / "supervisor.py")
supervisor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = supervisor
spec.loader.exec_module(supervisor)


class SupervisorContractTests(unittest.TestCase):
    def test_gateway_surface_owns_proxy(self):
        # 0.22.4: Desktop chats THROUGH the gateway service, so the gateway is the
        # process that serves the request and must own the relay.
        self.assertTrue(supervisor.should_manage_proxy(["hermes", "gateway", "run"]))

    def test_one_shot_workers_do_not_own_proxy(self):
        self.assertFalse(supervisor.should_manage_proxy(["hermes", "cron", "run"]))
        self.assertFalse(supervisor.should_manage_proxy(["hermes", "send", "-q", "hi"]))
        self.assertFalse(supervisor.should_manage_proxy(["hermes", "kanban", "serve-worker"]))

    def test_desktop_and_chat_surfaces_can_manage_proxy(self):
        self.assertTrue(supervisor.should_manage_proxy(["hermes", "serve", "--host", "127.0.0.1"]))
        self.assertTrue(supervisor.should_manage_proxy(["hermes", "chat", "-q", "hello"]))
        self.assertTrue(supervisor.should_manage_proxy(["hermes"]))

    def test_management_commands_do_not_start_proxy(self):
        for command in ("doctor", "status", "model", "auth", "config", "plugins", "setup"):
            with self.subTest(command=command):
                self.assertFalse(supervisor.should_manage_proxy(["hermes", command]))

    def test_provider_route_match_is_exact_to_relay(self):
        port = supervisor.PORT
        self.assertTrue(supervisor.is_relay_request("custom:opencode2api", f"http://127.0.0.1:{port}/v1"))
        self.assertTrue(supervisor.is_relay_request("opencode2api", f"http://127.0.0.1:{port}/v1/"))
        self.assertFalse(supervisor.is_relay_request("custom:api.closerouter.dev", "https://api.closerouter.dev/v1"))

    def test_platform_asset_mapping(self):
        cases = (
            ("Linux", "x86_64", "linux_amd64"),
            ("Linux", "aarch64", "linux_arm64"),
            ("Darwin", "arm64", "darwin_arm64"),
            ("Windows", "AMD64", "windows_amd64"),
        )
        for os_name, arch, suffix in cases:
            with self.subTest(os=os_name, arch=arch):
                self.assertIn(suffix, supervisor.release_asset(os_name, arch, supervisor.VERSION))

    def test_unsupported_platform_is_explicit(self):
        with self.assertRaises(supervisor.UnsupportedPlatform):
            supervisor.release_asset("Plan9", "mips", supervisor.VERSION)

    def test_idle_window_parks_only_an_owned_relay(self):
        self.assertFalse(supervisor.idle_expired(now=1000.0, last_activity=500.0, idle_seconds=0))
        self.assertFalse(supervisor.idle_expired(now=1000.0, last_activity=500.0, idle_seconds=600))
        self.assertTrue(supervisor.idle_expired(now=1000.0, last_activity=300.0, idle_seconds=600))

    def test_checksum_parser_requires_exact_asset_and_sha256(self):
        asset = f"opencode2api_v{supervisor.VERSION}_linux_amd64.tar.gz"
        digest = "a" * 64
        self.assertEqual(supervisor.checksum_for(f"{digest}  ./{asset}\n", asset), digest)
        for text in (f"{'a' * 63}  ./{asset}\n", f"{'b' * 64}  ./other.tar.gz\n"):
            with self.subTest(text=text):
                with self.assertRaises(supervisor.InvalidChecksum):
                    supervisor.checksum_for(text, asset)


if __name__ == "__main__":
    unittest.main()
