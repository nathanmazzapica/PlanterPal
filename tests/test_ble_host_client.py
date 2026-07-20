import ast
import importlib.util
import json
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = PROJECT_ROOT / "tools" / "ble_provision_client.py"
PROVISIONER_PATH = PROJECT_ROOT / "lib" / "ble_provisioning.py"


def import_client():
    spec = importlib.util.spec_from_file_location(
        "ble_provision_client_under_test",
        CLIENT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = import_client()


def string_constants(path, names):
    tree = ast.parse(path.read_text(), filename=str(path))
    constants = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id in names
            and isinstance(node.value, ast.Constant)
        ):
            constants[target.id] = node.value.value
    return constants


class BleHostClientTests(unittest.TestCase):
    def test_protocol_identifiers_match_firmware(self):
        names = {"SERVICE_UUID", "COMMAND_CHAR_UUID", "STATUS_CHAR_UUID"}
        self.assertEqual(
            string_constants(CLIENT_PATH, names),
            string_constants(PROVISIONER_PATH, names),
        )

    def test_command_matches_firmware_contract_without_logging_helpers(self):
        payload = client.build_command("garden", "top-secret")
        self.assertEqual(
            json.loads(payload.decode("utf-8")),
            {
                "type": "wifi_credentials",
                "ssid": "garden",
                "password": "top-secret",
            },
        )
        self.assertLessEqual(len(payload), client.MAX_PAYLOAD_BYTES)

    def test_utf8_boundaries_and_invalid_inputs_fail_locally(self):
        client.build_command("é" * 16, "ø" * 32)
        cases = (
            ("", "password", ValueError),
            ("é" * 17, "password", ValueError),
            ("garden", "ø" * 33, ValueError),
            ("garden", None, TypeError),
        )
        for ssid, password, error in cases:
            with self.subTest(ssid=ssid, password_type=type(password)):
                with self.assertRaises(error):
                    client.build_command(ssid, password)

    def test_status_decoder_rejects_malformed_or_untyped_values(self):
        self.assertEqual(
            client.decode_status(b'{"status":"ready"}'),
            {"status": "ready"},
        )
        for payload in (b"not json", b"[]", b'{"status":1}'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    client.decode_status(payload)


if __name__ == "__main__":
    unittest.main()
