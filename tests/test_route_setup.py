import importlib.util
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("opencode2api_plugin_route", PLUGIN / "route_setup.py")
route_setup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = route_setup
spec.loader.exec_module(route_setup)


class RouteSetupTests(unittest.TestCase):
    def test_appends_to_existing_list(self):
        cfg = {"custom_providers": [{"name": "Api.closerouter.dev",
                                     "base_url": "https://api.example.com/v1"}]}
        out, changed = route_setup.ensure_route_entry(cfg)
        self.assertTrue(changed)
        self.assertEqual(len(out["custom_providers"]), 2)
        self.assertEqual(out["custom_providers"][0]["name"], "Api.closerouter.dev")
        self.assertEqual(out["custom_providers"][1]["name"], route_setup.ROUTE_NAME)
        self.assertEqual(out["custom_providers"][1]["base_url"], route_setup.ROUTE_BASE_URL)
        self.assertEqual(out["custom_providers"][1]["api_key"], "public")

    def test_noop_when_route_present(self):
        cfg = {"custom_providers": [dict(route_setup.ROUTE_ENTRY)]}
        out, changed = route_setup.ensure_route_entry(cfg)
        self.assertFalse(changed)
        self.assertEqual(len(out["custom_providers"]), 1)

    def test_noop_when_present_in_new_style_providers(self):
        cfg = {"providers": {route_setup.ROUTE_NAME: {
            "api": route_setup.ROUTE_BASE_URL, "key_env": "OPENCODE2API_API_KEY"}}}
        out, changed = route_setup.ensure_route_entry(cfg)
        self.assertFalse(changed)
        self.assertNotIn("custom_providers", out)

    def test_creates_block_when_absent(self):
        cfg: dict = {}
        out, changed = route_setup.ensure_route_entry(cfg)
        self.assertTrue(changed)
        self.assertEqual(len(out["custom_providers"]), 1)
        self.assertEqual(out["custom_providers"][0]["name"], route_setup.ROUTE_NAME)

    def test_never_touches_non_list_custom_providers(self):
        cfg = {"custom_providers": {"not": "a list"}}
        out, changed = route_setup.ensure_route_entry(cfg)
        self.assertFalse(changed)
        self.assertEqual(out["custom_providers"], {"not": "a list"})

    def test_name_match_is_case_insensitive(self):
        cfg = {"custom_providers": [{"name": "OpenCode2Api", "base_url": "x"}]}
        out, changed = route_setup.ensure_route_entry(cfg)
        self.assertFalse(changed)


class RelayContractTests(unittest.TestCase):
    """Import the supervisor module too so both module contracts stay in CI."""

    def test_supervisor_module_loads(self):
        spec = importlib.util.spec_from_file_location(
            "opencode2api_plugin_supervisor", PLUGIN / "supervisor.py")
        sup = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = sup
        spec.loader.exec_module(sup)
        self.assertTrue(sup.is_relay_request("custom:opencode2api", "http://127.0.0.1:8000/v1"))
        self.assertFalse(sup.is_relay_request("custom:api.closerouter.dev", "https://api.closerouter.dev/v1"))


if __name__ == "__main__":
    unittest.main()
