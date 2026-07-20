import ast
import importlib
import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_PATH = PROJECT_ROOT / "web" / "credentials.py"


class FakeNVS:
    """Host-side model of the small esp32.NVS surface the store may use."""

    def __init__(
        self,
        record=None,
        get_error=None,
        set_error=None,
        erase_error=None,
        commit_error=None,
        reported_length=None,
    ):
        self.record = record
        self.get_error = get_error
        self.set_error = set_error
        self.erase_error = erase_error
        self.commit_error = commit_error
        self.reported_length = reported_length
        self.calls = []
        self.read_buffer_sizes = []

    def get_blob(self, key, buffer):
        self.calls.append(("get_blob", key))
        self.read_buffer_sizes.append(len(buffer))
        if not isinstance(buffer, bytearray):
            raise AssertionError("NVS.get_blob requires a bytearray")
        if self.get_error is not None:
            raise self.get_error
        if self.record is None:
            raise OSError("missing NVS key")
        if len(self.record) > len(buffer):
            raise OSError("NVS blob buffer too small")

        buffer[:len(self.record)] = self.record
        if self.reported_length is not None:
            return self.reported_length
        return len(self.record)

    def set_blob(self, key, value):
        payload = bytes(value)
        self.calls.append(("set_blob", key, payload))
        if self.set_error is not None:
            raise self.set_error
        self.record = payload

    def erase_key(self, key):
        self.calls.append(("erase_key", key))
        if self.erase_error is not None:
            raise self.erase_error
        self.record = None

    def commit(self):
        self.calls.append(("commit",))
        if self.commit_error is not None:
            raise self.commit_error


def import_credentials_module():
    created = []
    esp32 = types.ModuleType("esp32")

    def make_nvs(namespace):
        nvs = FakeNVS()
        created.append((namespace, nvs))
        return nvs

    esp32.NVS = make_nvs
    old_esp32 = sys.modules.get("esp32")
    old_credentials = sys.modules.get("web.credentials")
    sys.modules["esp32"] = esp32
    sys.modules.pop("web.credentials", None)

    try:
        module = importlib.import_module("web.credentials")
    finally:
        if old_esp32 is None:
            sys.modules.pop("esp32", None)
        else:
            sys.modules["esp32"] = old_esp32

        if old_credentials is None:
            sys.modules.pop("web.credentials", None)
        else:
            sys.modules["web.credentials"] = old_credentials

    return module, created


credentials_module, DEFAULT_NVS_INSTANCES = import_credentials_module()
Credentials = credentials_module.Credentials
CredentialStore = credentials_module.CredentialStore


def encoded_record(version=1, ssid="garden", password="secret", **extra):
    value = {
        "version": version,
        "ssid": ssid,
        "password": password,
    }
    value.update(extra)
    return json.dumps(value).encode("utf-8")


class CredentialsValueTests(unittest.TestCase):
    def test_credentials_expose_read_only_versioned_fields(self):
        credentials = Credentials("garden", "secret", version=1)

        self.assertEqual(credentials.version, 1)
        self.assertEqual(credentials.ssid, "garden")
        self.assertEqual(credentials.password, "secret")

        for attribute, replacement in (
            ("version", 2),
            ("ssid", "other"),
            ("password", "other-secret"),
        ):
            with self.subTest(attribute=attribute):
                with self.assertRaises((AttributeError, TypeError)):
                    setattr(credentials, attribute, replacement)

        for attribute in ("version", "ssid", "password", "_password"):
            with self.subTest(deleted_attribute=attribute):
                with self.assertRaises((AttributeError, TypeError)):
                    delattr(credentials, attribute)

        with self.assertRaises((AttributeError, TypeError)):
            credentials._password = "replacement-secret"

    def test_repr_and_str_never_reveal_password(self):
        password = "raw-password-MUST-NOT-LEAK"
        credentials = Credentials("garden", password, version=1)

        self.assertNotIn(password, repr(credentials))
        self.assertNotIn(password, str(credentials))

    def test_empty_ssid_is_rejected_but_empty_password_is_allowed(self):
        with self.assertRaises((TypeError, ValueError)):
            Credentials("", "secret", version=1)

        credentials = Credentials("open-network", "", version=1)
        self.assertEqual(credentials.password, "")

    def test_ssid_limit_is_measured_in_utf8_bytes(self):
        self.assertEqual(len("e" * 32), 32)
        self.assertEqual(len(("é" * 16).encode("utf-8")), 32)

        Credentials("e" * 32, "", version=1)
        Credentials("é" * 16, "", version=1)

        for ssid in ("e" * 33, "é" * 17):
            with self.subTest(ssid_length=len(ssid.encode("utf-8"))):
                with self.assertRaises((TypeError, ValueError)):
                    Credentials(ssid, "", version=1)

    def test_password_limit_is_measured_in_utf8_bytes(self):
        Credentials("garden", "p" * 64, version=1)
        Credentials("garden", "🔒" * 16, version=1)

        for password in ("p" * 65, "🔒" * 17):
            with self.subTest(password_length=len(password.encode("utf-8"))):
                with self.assertRaises((TypeError, ValueError)):
                    Credentials("garden", password, version=1)

    def test_ssid_password_and_version_have_required_types(self):
        for arguments in (
            (b"garden", "secret", 1),
            ("garden", b"secret", 1),
            ("garden", "secret", "1"),
            ("garden", "secret", True),
            ("garden", "secret", 1.0),
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises((TypeError, ValueError)):
                    Credentials(arguments[0], arguments[1], version=arguments[2])


class CredentialStoreTests(unittest.TestCase):
    def test_injected_nvs_keeps_constructor_host_safe_and_side_effect_free(self):
        nvs = FakeNVS(record=encoded_record())

        store = CredentialStore(nvs=nvs)

        self.assertEqual(nvs.calls, [])
        self.assertIsNotNone(store)

    def test_default_constructor_creates_one_nvs_without_writing(self):
        created = []
        esp32 = types.ModuleType("esp32")

        def make_nvs(namespace):
            nvs = FakeNVS()
            created.append((namespace, nvs))
            return nvs

        esp32.NVS = make_nvs
        previous = sys.modules.get("esp32")
        sys.modules["esp32"] = esp32
        try:
            store = CredentialStore()
        finally:
            if previous is None:
                sys.modules.pop("esp32", None)
            else:
                sys.modules["esp32"] = previous

        self.assertIsNotNone(store)
        self.assertEqual(len(created), 1)
        _, nvs = created[0]
        self.assertEqual(nvs.calls, [])

    def test_load_returns_versioned_immutable_credentials(self):
        nvs = FakeNVS(record=encoded_record(ssid="garden", password="secret"))
        store = CredentialStore(nvs=nvs)

        credentials = store.load()

        self.assertIsInstance(credentials, Credentials)
        self.assertEqual(credentials.version, 1)
        self.assertEqual(credentials.ssid, "garden")
        self.assertEqual(credentials.password, "secret")
        self.assertEqual(len(nvs.calls), 1)
        self.assertEqual(nvs.calls[0][0], "get_blob")

    def test_missing_record_loads_as_no_credentials_without_writing(self):
        nvs = FakeNVS(record=None)
        store = CredentialStore(nvs=nvs)

        self.assertIsNone(store.load())
        self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_any_get_blob_oserror_loads_as_no_credentials(self):
        error = OSError("NVS driver failed")
        nvs = FakeNVS(get_error=error)

        self.assertIsNone(CredentialStore(nvs=nvs).load())
        self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_corrupt_short_and_wrong_shape_records_load_as_none_without_repair(self):
        records = (
            b"",
            b"{",
            b"{}",
            b"[]",
            b"null",
            b"\xff\xfe",
            json.dumps({"version": 1, "ssid": "garden"}).encode("utf-8"),
            json.dumps(
                {"version": 1, "ssid": 7, "password": ["secret"]}
            ).encode("utf-8"),
        )

        for record in records:
            with self.subTest(record=record):
                nvs = FakeNVS(record=record)
                self.assertIsNone(CredentialStore(nvs=nvs).load())
                self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_version_mismatch_loads_as_none_without_erasing_or_committing(self):
        nvs = FakeNVS(record=encoded_record(version=999))

        self.assertIsNone(CredentialStore(nvs=nvs).load())
        self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_boolean_and_float_versions_do_not_alias_current_integer_version(self):
        for corrupt_version in (True, 1.0):
            with self.subTest(corrupt_version=corrupt_version):
                nvs = FakeNVS(record=encoded_record(version=corrupt_version))
                self.assertIsNone(CredentialStore(nvs=nvs).load())
                self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_load_uses_actual_returned_byte_count(self):
        complete = encoded_record()
        nvs = FakeNVS(record=complete, reported_length=len(complete) - 1)

        self.assertIsNone(CredentialStore(nvs=nvs).load())
        self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_load_ignores_bytes_beyond_exact_returned_length(self):
        complete = encoded_record()
        nvs = FakeNVS(
            record=complete + b"trailing-corruption",
            reported_length=len(complete),
        )

        loaded = CredentialStore(nvs=nvs).load()

        self.assertEqual(loaded, Credentials("garden", "secret", version=1))
        self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_impossible_reported_lengths_are_rejected_without_writing(self):
        record = encoded_record()

        for reported_length in (-1, 10_000):
            with self.subTest(reported_length=reported_length):
                nvs = FakeNVS(record=record, reported_length=reported_length)
                self.assertIsNone(CredentialStore(nvs=nvs).load())
                self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])

    def test_oversized_blob_loads_as_none_with_a_bounded_read_buffer(self):
        nvs = FakeNVS(record=b"x" * 4096)

        self.assertIsNone(CredentialStore(nvs=nvs).load())
        self.assertEqual([call[0] for call in nvs.calls], ["get_blob"])
        self.assertEqual(len(nvs.read_buffer_sizes), 1)
        self.assertGreater(nvs.read_buffer_sizes[0], 0)
        self.assertLess(nvs.read_buffer_sizes[0], len(nvs.record))

    def test_save_writes_exactly_one_versioned_record_then_commits(self):
        nvs = FakeNVS()
        store = CredentialStore(nvs=nvs)
        credentials = Credentials("garden", "secret", version=1)

        result = store.save(credentials)

        self.assertIsNone(result)
        self.assertEqual([call[0] for call in nvs.calls], ["set_blob", "commit"])
        _, key, payload = nvs.calls[0]
        self.assertIsInstance(key, str)
        self.assertEqual(
            json.loads(payload.decode("utf-8")),
            {"version": 1, "ssid": "garden", "password": "secret"},
        )

    def test_save_rejects_unsupported_version_before_any_write(self):
        nvs = FakeNVS()
        credentials = Credentials("garden", "secret", version=999)

        with self.assertRaises((TypeError, ValueError)):
            CredentialStore(nvs=nvs).save(credentials)

        self.assertEqual(nvs.calls, [])

    def test_save_rejects_non_credentials_before_any_write(self):
        nvs = FakeNVS()

        with self.assertRaises((TypeError, ValueError)):
            CredentialStore(nvs=nvs).save(
                {"ssid": "garden", "password": "secret", "version": 1}
            )

        self.assertEqual(nvs.calls, [])

    def test_set_blob_failure_propagates_and_never_commits(self):
        failure = OSError("set failed")
        nvs = FakeNVS(set_error=failure)

        with self.assertRaises(OSError) as raised:
            CredentialStore(nvs=nvs).save(
                Credentials("garden", "secret", version=1)
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual([call[0] for call in nvs.calls], ["set_blob"])

    def test_commit_failure_from_save_propagates(self):
        failure = OSError("commit failed")
        nvs = FakeNVS(commit_error=failure)

        with self.assertRaises(OSError) as raised:
            CredentialStore(nvs=nvs).save(
                Credentials("garden", "secret", version=1)
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual([call[0] for call in nvs.calls], ["set_blob", "commit"])

    def test_clear_erases_the_same_record_key_and_commits(self):
        nvs = FakeNVS()
        store = CredentialStore(nvs=nvs)
        store.save(Credentials("garden", "secret", version=1))
        saved_key = nvs.calls[0][1]
        nvs.calls.clear()

        result = store.clear()

        self.assertIsNone(result)
        self.assertEqual(
            nvs.calls,
            [("erase_key", saved_key), ("commit",)],
        )

    def test_erase_failure_propagates_and_never_commits(self):
        failure = OSError("erase failed")
        nvs = FakeNVS(erase_error=failure)

        with self.assertRaises(OSError) as raised:
            CredentialStore(nvs=nvs).clear()

        self.assertIs(raised.exception, failure)
        self.assertEqual([call[0] for call in nvs.calls], ["erase_key"])

    def test_commit_failure_from_clear_propagates(self):
        failure = OSError("commit failed")
        nvs = FakeNVS(commit_error=failure)

        with self.assertRaises(OSError) as raised:
            CredentialStore(nvs=nvs).clear()

        self.assertIs(raised.exception, failure)
        self.assertEqual([call[0] for call in nvs.calls], ["erase_key", "commit"])

    def test_unicode_at_byte_limits_round_trips_through_one_blob(self):
        ssid = "é" * 16
        password = "🔒" * 16
        nvs = FakeNVS()
        store = CredentialStore(nvs=nvs)

        store.save(Credentials(ssid, password, version=1))
        loaded = store.load()

        self.assertEqual(loaded.version, 1)
        self.assertEqual(loaded.ssid, ssid)
        self.assertEqual(loaded.password, password)
        self.assertEqual(
            [call[0] for call in nvs.calls],
            ["set_blob", "commit", "get_blob"],
        )

    def test_password_and_raw_payload_are_never_printed_on_success_or_corruption(self):
        password = "raw-password-MUST-NOT-LEAK"
        nvs = FakeNVS()
        store = CredentialStore(nvs=nvs)
        output = io.StringIO()

        with redirect_stdout(output), redirect_stderr(output):
            store.save(Credentials("garden", password, version=1))
            store.load()
            nvs.record = (
                b'{"version":1,"ssid":"garden","password":"'
                + password.encode("utf-8")
            )
            store.load()

        emitted = output.getvalue()
        self.assertNotIn(password, emitted)
        self.assertEqual(emitted, "")


class CredentialStoreStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(
            CREDENTIALS_PATH.read_text(),
            filename=str(CREDENTIALS_PATH),
        )

    def test_store_has_no_ble_wlan_led_display_or_logging_responsibilities(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden_prefixes = (
            "aioble",
            "bluetooth",
            "network",
            "machine",
            "neopixel",
            "led",
            "display",
            "lib.ble_provisioning",
            "web.wifi",
            "logging",
        )
        violations = sorted(
            name
            for name in imported
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in forbidden_prefixes
            )
        )
        self.assertEqual(violations, [])

        print_calls = [
            node.lineno
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ]
        self.assertEqual(print_calls, [], "CredentialStore must never print secrets")

    def test_nvs_mutation_is_scoped_to_credential_store(self):
        methods = {"get_blob", "set_blob", "erase_key", "commit"}
        violations = []

        class NVSVisitor(ast.NodeVisitor):
            def __init__(self):
                self.class_name = None

            def visit_ClassDef(self, node):
                previous = self.class_name
                self.class_name = node.name
                self.generic_visit(node)
                self.class_name = previous

            def visit_Call(self, node):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in methods
                    and self.class_name != "CredentialStore"
                ):
                    violations.append((node.lineno, node.func.attr, self.class_name))
                self.generic_visit(node)

        NVSVisitor().visit(self.tree)
        self.assertEqual(violations, [])

    def test_no_other_production_module_accesses_nvs_directly(self):
        nvs_methods = {"get_blob", "set_blob", "erase_key"}
        violations = []

        for path in PROJECT_ROOT.rglob("*.py"):
            if (
                path == CREDENTIALS_PATH
                or "tests" in path.parts
                or ".venv" in path.parts
                or path.name == "boot.py"
            ):
                continue

            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "esp32" for alias in node.names):
                        violations.append((str(path.relative_to(PROJECT_ROOT)), node.lineno, "esp32"))
                elif isinstance(node, ast.ImportFrom) and node.module == "esp32":
                    violations.append((str(path.relative_to(PROJECT_ROOT)), node.lineno, "esp32"))
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in nvs_methods
                ):
                    violations.append(
                        (
                            str(path.relative_to(PROJECT_ROOT)),
                            node.lineno,
                            node.func.attr,
                        )
                    )

        self.assertEqual(
            violations,
            [],
            "all NVS access must remain behind web.credentials.CredentialStore",
        )

    def test_runtime_credential_imports_are_limited_to_composition_and_coordinator(self):
        importers = []

        for path in PROJECT_ROOT.rglob("*.py"):
            if (
                path == CREDENTIALS_PATH
                or "tests" in path.parts
                or ".venv" in path.parts
                or path.name == "boot.py"
            ):
                continue

            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == "web.credentials" for alias in node.names):
                        importers.append(
                            (
                                str(path.relative_to(PROJECT_ROOT)),
                                node.lineno,
                                ("*",),
                            )
                        )
                elif isinstance(node, ast.ImportFrom) and node.module == "web.credentials":
                    importers.append(
                        (
                            str(path.relative_to(PROJECT_ROOT)),
                            node.lineno,
                            tuple(alias.name for alias in node.names),
                        )
                    )

        unexpected = [
            item
            for item in importers
            if item[0] not in {"main.py", "app/provisioning.py"}
        ]
        self.assertEqual(
            unexpected,
            [],
            "only the composition root and provisioning coordinator may import credentials",
        )

        symbols_by_path = {
            path: set(symbols)
            for path, _, symbols in importers
        }
        self.assertIn("app/provisioning.py", symbols_by_path)
        self.assertIn("main.py", symbols_by_path)
        self.assertEqual(symbols_by_path["app/provisioning.py"], {"Credentials"})
        self.assertTrue(
            symbols_by_path["main.py"].issubset({"CredentialStore", "Credentials"})
        )
        self.assertIn("CredentialStore", symbols_by_path["main.py"])


if __name__ == "__main__":
    unittest.main()
