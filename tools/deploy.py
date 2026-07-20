#!/usr/bin/env python3
"""Deploy the complete PlanterPal device graph with ``main.py`` copied last.

This is the canonical deployment manifest and workflow. Run ``--dry-run`` to
validate local import closure and inspect the plan without contacting a board.
"""

import argparse
import ast
import shlex
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MIN_MICROPYTHON_VERSION = (1, 28, 0)
SUPPORTED_IMPLEMENTATION = "micropython"
SUPPORTED_PLATFORM = "esp32"
RECOVERY_DELAY_S = "1"
COMMAND_TIMEOUT_S = 120
RESET_SETTLE_S = 2

DEVICE_DIRECTORIES = (
    "app",
    "display",
    "led",
    "lib",
    "sensors",
    "web",
)

# Keep this explicit. validate_manifest() compares it with the production
# source tree and checks the complete local import closure.
SUPPORT_FILES = (
    "app/application.py",
    "app/provisioning.py",
    "app/provisioning_runtime.py",
    "app/state.py",
    "config.py",
    "device_hardware.py",
    "display/display.py",
    "display/null_display.py",
    "display/probe.py",
    "led/controller.py",
    "led/provisioning_indicator.py",
    "lib/async_channel.py",
    "lib/backlight_driver.py",
    "lib/bh1750.py",
    "lib/ble_bootstrap.py",
    "lib/ble_provisioning.py",
    "lib/ek1940.py",
    "lib/hd44780.py",
    "lib/hd44780_4bit_driver.py",
    "lib/hd44780_4bit_payload.py",
    "lib/lcd.py",
    "lib/pcf8574.py",
    "lib/ws2811b.py",
    "sensors/light.py",
    "sensors/moisture.py",
    "web/client.py",
    "web/credentials.py",
    "web/exceptions.py",
    "web/network_config.py",
    "web/reporter.py",
    "web/wifi.py",
)

ENTRY_POINT = "main.py"

HARDWARE_PROBES = (
    "tests/hardware/application_composition_hardware_probe.py",
    "tests/hardware/running_import_hardware_probe.py",
)

EXTERNAL_MODULES = frozenset(
    {
        "aioble",
        "asyncio",
        "bluetooth",
        "errno",
        "esp32",
        "gc",
        "json",
        "machine",
        "micropython",
        "neopixel",
        "network",
        "sys",
        "time",
        "utime",
    }
)

# Fresh deployments intentionally omit the gitignored legacy host fallback.
OPTIONAL_LOCAL_MODULES = frozenset({"web.wifi_config"})
EXCLUDED_DEVICE_SOURCES = frozenset(
    {"web/wifi_config.example.py", "web/wifi_config.py"}
)

Step = namedtuple("Step", "name detail")


class DeploymentError(RuntimeError):
    pass


def discover_device_sources():
    sources = {ENTRY_POINT, "config.py", "device_hardware.py"}
    for directory in DEVICE_DIRECTORIES:
        sources.update(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / directory).glob("*.py")
        )
    return sources - EXCLUDED_DEVICE_SOURCES


def _module_names(path):
    module = path[:-3].replace("/", ".")
    names = {module}
    if path.startswith("lib/"):
        # MicroPython adds /lib to sys.path, and the vendored LCD stack uses
        # same-directory imports without a lib. prefix.
        names.add(module.split(".")[-1])
    return names


def _local_module_map(files):
    modules = {}
    for path in files:
        for name in _module_names(path):
            modules[name] = path
    return modules


def missing_local_dependencies(files):
    files = tuple(files)
    known_sources = discover_device_sources()
    known_modules = _local_module_map(known_sources)
    included = set(files)
    missing = []

    for path in files:
        tree = ast.parse(
            (PROJECT_ROOT / path).read_text(encoding="utf-8"),
            filename=path,
        )
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Import):
                candidates.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                candidates.append(node.module)
                candidates.extend(
                    node.module + "." + alias.name for alias in node.names
                )

            for name in candidates:
                if name in EXTERNAL_MODULES or name in OPTIONAL_LOCAL_MODULES:
                    continue
                dependency = known_modules.get(name)
                if dependency is not None and dependency not in included:
                    missing.append((path, name, dependency))

    return sorted(set(missing))


def validate_manifest(support_files=SUPPORT_FILES, entry_point=ENTRY_POINT):
    support_files = tuple(support_files)
    all_files = support_files + (entry_point,)

    if len(all_files) != len(set(all_files)):
        raise DeploymentError("deployment manifest contains duplicate paths")
    if "boot.py" in all_files:
        raise DeploymentError("boot.py must never appear in the deployment manifest")

    forbidden = sorted(
        path
        for path in all_files
        if path.startswith(("tests/", "tools/", "typings/"))
        or "__pycache__" in path
        or path.endswith("wifi_config.py")
    )
    if forbidden:
        raise DeploymentError(
            "host-only, generated, or secret paths in manifest: "
            + ", ".join(forbidden)
        )

    missing_files = sorted(
        path for path in all_files if not (PROJECT_ROOT / path).is_file()
    )
    if missing_files:
        raise DeploymentError(
            "manifest source files do not exist: " + ", ".join(missing_files)
        )

    discovered = discover_device_sources()
    declared = set(all_files)
    omitted = sorted(discovered - declared)
    undeclared = sorted(declared - discovered)
    if omitted or undeclared:
        details = []
        if omitted:
            details.append("omitted production files: " + ", ".join(omitted))
        if undeclared:
            details.append("non-production files: " + ", ".join(undeclared))
        raise DeploymentError("; ".join(details))

    missing_imports = missing_local_dependencies(all_files)
    if missing_imports:
        details = [
            "{} imports {} from omitted {}".format(source, module, dependency)
            for source, module, dependency in missing_imports
        ]
        raise DeploymentError("missing local import dependencies: " + "; ".join(details))

    return all_files


def build_plan(clean=False, reset=True):
    steps = [
        Step("check_firmware", None),
        Step("remove_entry_point", ENTRY_POINT),
    ]
    if clean:
        steps.extend(
            (
                Step("clean_managed_files", SUPPORT_FILES),
                Step("verify_clean", SUPPORT_FILES + (ENTRY_POINT,)),
            )
        )
    steps.extend(
        (
            Step("ensure_aioble", "aioble"),
            Step("create_directories", DEVICE_DIRECTORIES),
            Step("copy_support", SUPPORT_FILES),
            Step("verify_support", SUPPORT_FILES),
        )
    )
    for probe in HARDWARE_PROBES:
        steps.extend(
            (
                Step("hard_reset_for_probe", probe),
                Step("run_probe", probe),
            )
        )
    steps.extend(
        (
            Step("copy_entry_point", ENTRY_POINT),
            Step("verify_entry_point", (ENTRY_POINT,)),
        )
    )
    if reset:
        steps.append(Step("reset", None))
    return tuple(steps)


def _remote_path(path):
    return ":" + path


def _remove_code(files, directories=()):
    return """
import os

def remove_tree(path):
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        child = path + "/" + name
        try:
            remove_tree(child)
            os.rmdir(child)
        except OSError:
            try:
                os.remove(child)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass

for path in {!r}:
    try:
        os.remove(path)
    except OSError:
        pass
for path in {!r}:
    remove_tree(path)
""".format(tuple(files), tuple(directories))


def _verify_code(files, expect_present):
    expected = tuple(
        (path, (PROJECT_ROOT / path).stat().st_size) for path in files
    )
    return """
import os
bad = []
for path, size in {!r}:
    try:
        actual = os.stat(path)[6]
    except OSError:
        actual = None
    if {!r}:
        if actual != size:
            bad.append((path, size, actual))
    elif actual is not None:
        bad.append((path, None, actual))
if bad:
    raise RuntimeError("deployment verification failed: {{}}".format(bad))
print("PP_DEPLOY_VERIFY_OK")
""".format(expected, expect_present)


class DeviceSession:
    def __init__(self, port, runner=subprocess.run, printer=print):
        self.port = port
        self._runner = runner
        self._printer = printer

    def run(self, *commands, capture=False):
        # Opening the CP2102 port can reset the ESP32. Enter raw REPL during
        # main.py's cooperative recovery window rather than racing the app.
        argv = [
            "mpremote",
            "connect",
            self.port,
            "sleep",
            RECOVERY_DELAY_S,
        ] + list(commands)
        self._printer("+ " + shlex.join(argv))
        try:
            completed = self._runner(
                argv,
                cwd=PROJECT_ROOT,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.STDOUT if capture else None,
                timeout=COMMAND_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            raise DeploymentError(
                "command exceeded {} seconds".format(COMMAND_TIMEOUT_S)
            )
        if completed.returncode:
            output = completed.stdout.strip() if completed.stdout else ""
            raise DeploymentError(
                "command failed with status {}{}".format(
                    completed.returncode,
                    ": " + output if output else "",
                )
            )
        return completed.stdout or ""

    def execute(self, code, capture=False):
        return self.run("exec", code, capture=capture)


def _check_firmware(session):
    code = (
        "import sys; v=sys.implementation.version; "
        "print('PP_FIRMWARE|{}|{}|{}|{}|{}'.format("
        "sys.implementation.name,sys.platform,v[0],v[1],v[2]))"
    )
    output = session.execute(code, capture=True)
    marker = next(
        (line for line in output.splitlines() if line.startswith("PP_FIRMWARE|")),
        None,
    )
    if marker is None:
        raise DeploymentError("could not parse firmware identity")

    _, implementation, platform, major, minor, patch = marker.split("|")
    version = (int(major), int(minor), int(patch))
    if implementation != SUPPORTED_IMPLEMENTATION:
        raise DeploymentError("device is not running MicroPython")
    if platform != SUPPORTED_PLATFORM:
        raise DeploymentError("device platform is not ESP32")
    if version < MIN_MICROPYTHON_VERSION:
        raise DeploymentError(
            "MicroPython {}.{}.{} or newer is required".format(
                *MIN_MICROPYTHON_VERSION
            )
        )
    print(
        "PASS firmware {} {}.{}.{}".format(platform, *version)
    )


def _ensure_aioble(session):
    # Importing aioble activates the ESP32 BLE controller. Verification here
    # must remain filesystem-only so the dedicated provisioning composition
    # probe is the first process to reserve NimBLE heap.
    verify = """
import os
found = False
for path in ("lib/aioble/__init__.mpy", "lib/aioble/__init__.py"):
    try:
        os.stat(path)
        found = True
    except OSError:
        pass
if not found:
    raise ImportError("aioble package is absent")
print("PP_AIOBLE_FILES_OK")
"""
    try:
        session.execute(verify, capture=True)
    except DeploymentError:
        session.run("mip", "install", "aioble")
        session.execute(verify, capture=True)
    print("PASS aioble package present; functional import deferred to BLE probe")


def _create_directories(session):
    code = """
import os
for path in {!r}:
    try:
        os.mkdir(path)
    except OSError:
        pass
print("PP_DEPLOY_DIRS_OK")
""".format(DEVICE_DIRECTORIES)
    session.execute(code)


def _copy_files(session, files):
    commands = []
    for path in files:
        commands.extend(("fs", "cp", path, _remote_path(path), "+"))
    commands.pop()
    session.run(*commands)


def _clean_managed_files(session):
    root_files = tuple(
        path for path in SUPPORT_FILES if "/" not in path
    ) + (ENTRY_POINT,)
    lib_files = tuple(path for path in SUPPORT_FILES if path.startswith("lib/"))
    managed_directories = tuple(
        directory for directory in DEVICE_DIRECTORIES if directory != "lib"
    )
    session.execute(
        _remove_code(root_files + lib_files, managed_directories)
    )


def execute_plan(plan, session):
    for step in plan:
        print("==> " + step.name)
        if step.name == "check_firmware":
            _check_firmware(session)
        elif step.name == "remove_entry_point":
            session.execute(_remove_code((ENTRY_POINT,)))
        elif step.name == "clean_managed_files":
            _clean_managed_files(session)
        elif step.name == "verify_clean":
            session.execute(_verify_code(step.detail, expect_present=False))
        elif step.name == "ensure_aioble":
            _ensure_aioble(session)
        elif step.name == "create_directories":
            _create_directories(session)
        elif step.name == "copy_support":
            _copy_files(session, step.detail)
        elif step.name == "verify_support":
            session.execute(_verify_code(step.detail, expect_present=True))
        elif step.name == "hard_reset_for_probe":
            session.run("reset")
            time.sleep(RESET_SETTLE_S)
        elif step.name == "run_probe":
            output = session.run("run", step.detail, capture=True)
            print(output, end="" if output.endswith("\n") else "\n")
        elif step.name == "copy_entry_point":
            _copy_files(session, (step.detail,))
        elif step.name == "verify_entry_point":
            session.execute(_verify_code(step.detail, expect_present=True))
        elif step.name == "reset":
            session.run("reset")
        else:
            raise DeploymentError("unknown deployment step: " + step.name)


def print_plan(plan, port, printer=print):
    printer("Manifest valid: {} support files + {}".format(len(SUPPORT_FILES), ENTRY_POINT))
    printer("Target: " + port)
    for index, step in enumerate(plan, 1):
        if isinstance(step.detail, tuple):
            detail = " ({} items)".format(len(step.detail))
        elif step.detail:
            detail = " (" + str(step.detail) + ")"
        else:
            detail = ""
        printer("{:02d}. {}{}".format(index, step.name, detail))


def deploy(port, clean=False, assume_yes=False, reset=True, dry_run=False):
    validate_manifest()
    plan = build_plan(clean=clean, reset=reset)
    if dry_run:
        print_plan(plan, port)
        return

    if shutil.which("mpremote") is None:
        raise DeploymentError("mpremote is not installed or not on PATH")
    if clean and not assume_yes:
        raise DeploymentError("--clean requires --yes because it removes managed device files")

    execute_plan(plan, DeviceSession(port))
    print("PASS deployment complete; main.py was copied after both hardware probes")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="mpremote serial device")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="remove managed application files before deployment",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the destructive --clean operation",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="leave the verified device at the raw REPL instead of resetting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the plan without contacting a device",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        deploy(
            args.port,
            clean=args.clean,
            assume_yes=args.yes,
            reset=not args.no_reset,
            dry_run=args.dry_run,
        )
    except DeploymentError as error:
        print("ERROR:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
