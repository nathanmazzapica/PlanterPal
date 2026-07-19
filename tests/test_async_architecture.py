import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BH1750_PATH = PROJECT_ROOT / "lib" / "bh1750.py"
LIGHT_PATH = PROJECT_ROOT / "sensors" / "light.py"
STATE_PATH = PROJECT_ROOT / "app" / "state.py"
MAIN_PATH = PROJECT_ROOT / "main.py"
CONFIG_PATH = PROJECT_ROOT / "config.py"


def parse(path):
    return ast.parse(path.read_text(), filename=str(path))


def import_symbols(tree):
    symbols = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                symbols[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                symbols[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return symbols


def qualified_name(node, symbols):
    if isinstance(node, ast.Name):
        return symbols.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = qualified_name(node.value, symbols)
        if base is not None:
            return f"{base}.{node.attr}"
    return None


def function_named(tree, name):
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def assigned_name(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return None


def assigned_value(node):
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return node.value
    return None


class AsyncArchitectureTests(unittest.TestCase):
    def test_bh1750_driver_has_no_resolved_blocking_sleep_call(self):
        tree = parse(BH1750_PATH)
        symbols = import_symbols(tree)
        violations = []
        lux = function_named(tree, "lux")

        blocking_calls = {
            "time.sleep",
            "time.sleep_ms",
            "time.sleep_us",
            "utime.sleep",
            "utime.sleep_ms",
            "utime.sleep_us",
            "machine.lightsleep",
        }
        for node in ast.walk(lux):
            if not isinstance(node, ast.Call):
                continue
            name = qualified_name(node.func, symbols)
            if name in blocking_calls:
                violations.append((node.lineno, name))

        for node in ast.walk(lux):
            if isinstance(node, (ast.For, ast.While)) and not any(
                isinstance(descendant, ast.Await) for descendant in ast.walk(node)
            ):
                violations.append((node.lineno, type(node).__name__))

        self.assertEqual(
            violations,
            [],
            "BH1750 conversion must use a cooperative asyncio sleep",
        )

    def test_driver_and_config_do_not_construct_private_i2c_locks(self):
        violations = []
        for path in (BH1750_PATH, CONFIG_PATH):
            tree = parse(path)
            symbols = import_symbols(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = qualified_name(node.func, symbols)
                if name in {"asyncio.Lock", "uasyncio.Lock"}:
                    violations.append((path.name, node.lineno, name))

        self.assertEqual(
            violations,
            [],
            "the composition root, not the driver or config module, owns lock construction",
        )

    def test_sensor_pipeline_does_not_spawn_tasks(self):
        violations = []
        for path in (BH1750_PATH, LIGHT_PATH, STATE_PATH):
            tree = parse(path)
            symbols = import_symbols(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and qualified_name(node.func, symbols) in {
                    "asyncio.create_task",
                    "uasyncio.create_task",
                }:
                    violations.append((path.name, node.lineno))

        main_tree = parse(MAIN_PATH)
        main_symbols = import_symbols(main_tree)
        sensor_terms = {"lux", "light", "monitor", "sample", "sensor", "state"}
        for node in ast.walk(main_tree):
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, main_symbols) not in {
                "asyncio.create_task",
                "uasyncio.create_task",
            }:
                continue
            target = qualified_name(node.args[0].func, main_symbols) if node.args and isinstance(node.args[0], ast.Call) else None
            if target and any(term in target.lower() for term in sensor_terms):
                violations.append((MAIN_PATH.name, node.lineno))

        self.assertEqual(
            violations,
            [],
            "light, moisture, and aggregate sampling remain one owned operation",
        )

    def test_composition_root_creates_and_injects_one_lock_for_sensor_bus(self):
        tree = parse(MAIN_PATH)
        symbols = import_symbols(tree)
        create_application = function_named(tree, "create_application")
        lock_bindings = set()

        for node in create_application.body:
            value = assigned_value(node)
            name = assigned_name(node)
            if name is None or not isinstance(value, ast.Call):
                continue
            if qualified_name(value.func, symbols) in {"asyncio.Lock", "uasyncio.Lock"}:
                lock_bindings.add(name)

        self.assertTrue(
            lock_bindings,
            "create_application must create the lock for cfg.SENSOR_BUS",
        )

        bus_calls = []
        injected_lock_names = []
        for node in ast.walk(create_application):
            if not isinstance(node, ast.Call):
                continue
            if qualified_name(node.func, symbols) != "lib.bh1750.BH1750":
                continue

            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            bus_values = [
                value
                for value in values
                if qualified_name(value, symbols) == "config.SENSOR_BUS"
            ]
            if not bus_values:
                continue
            bus_calls.append(node)
            injected = {
                value.id
                for value in values
                if isinstance(value, ast.Name)
                and value.id in lock_bindings
            }
            self.assertEqual(
                len(injected),
                1,
                "every BH1750 using cfg.SENSOR_BUS must receive one composition-owned lock",
            )
            injected_lock_names.extend(injected)

        self.assertTrue(
            bus_calls,
            "create_application must construct BH1750 for cfg.SENSOR_BUS",
        )
        self.assertEqual(
            len(set(injected_lock_names)),
            1,
            "all BH1750 instances on cfg.SENSOR_BUS must share the same lock binding",
        )

    def test_application_run_loop_awaits_state_update(self):
        tree = parse(MAIN_PATH)
        symbols = import_symbols(tree)
        run_loop = function_named(tree, "_run_loop")
        awaited_state_updates = [
            node
            for node in ast.walk(run_loop)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and qualified_name(node.value.func, symbols) == "self.state.update"
        ]

        self.assertEqual(
            len(awaited_state_updates),
            1,
            "Application._run_loop must await exactly one aggregate State update per cycle",
        )


if __name__ == "__main__":
    unittest.main()
