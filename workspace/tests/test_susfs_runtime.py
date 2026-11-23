#!/usr/bin/env python3
"""Exercise the actual SUSFS functions with fault-injectable host kernel APIs.

The source-root option runs exactly the same correctness assertions against the
preserved baseline. This is a unit harness, not a device or full-kernel test.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = ROOT
FUNCTIONS = (
    "copy_config_to_buf",
    "susfs_get_enabled_features",
    "susfs_sus_kstat_spoof_generic_fillattr",
    "susfs_sus_kstat_spoof_show_map_vma",
    "susfs_open_redirect_spoof_do_sys_openat",
    "susfs_open_redirect_spoof_vfs_readlink",
    "susfs_open_redirect_spoof_do_proc_readlink",
    "susfs_open_redirect_spoof_vfs_statfs",
    "susfs_open_redirect_spoof_seq_show",
    "susfs_open_redirect_spoof_show_map_vma_srcu",
)
STRUCTURES = (
    "st_susfs_sus_kstat", "st_susfs_sus_kstat_hlist",
    "st_susfs_open_redirect", "st_susfs_open_redirect_hlist",
    "st_susfs_enabled_features",
)
RESULTS = []


def extract_block(text, pattern, terminator=""):
    match = re.search(pattern, text, re.M)
    if not match:
        raise ValueError(f"Source declaration missing: {pattern}")
    start = text.index("{", match.start())
    depth = 1
    end = start + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[match.start():end] + terminator


class SusfsRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="susfs-runtime-unit-")
        cls.addClassCleanup(cls.temporary.cleanup)
        folder = Path(cls.temporary.name)
        source = SOURCE_ROOT / "kernel/common/fs/susfs.c"
        header = SOURCE_ROOT / "kernel/common/include/linux/susfs.h"
        code = source.read_text()
        declarations = header.read_text()
        functions = "\n\n".join(extract_block(
            code, rf"^(?:static )?(?:int|void|struct filename \*)\s*{name}\([^;]*?\)\s*\{{"
        ) for name in FUNCTIONS)
        structures = "\n".join(extract_block(
            declarations, rf"^struct {name}\s*\{{", ";"
        ) for name in STRUCTURES)
        constants = "\n".join(re.findall(r"^#define KSTAT_.*$", declarations, re.M))
        harness = (ROOT / "tests/susfs_runtime_harness.c").read_text()
        harness = harness.replace("/* SOURCE_STRUCTURES */", structures + "\n" + constants)
        harness = harness.replace("/* SOURCE_FUNCTIONS */", functions)
        (folder / "runtime.c").write_text(harness)
        cls.program = folder / "runtime"
        compiler = shutil.which("cc")
        if compiler is None:
            raise RuntimeError("Host C compiler required for SUSFS unit tests")
        command = [compiler, "-std=gnu11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                   "-Wno-sign-compare", "-fsanitize=address,undefined",
                   "-fno-omit-frame-pointer", "-fno-pie", "-no-pie",
                   str(folder / "runtime.c"), "-o", str(cls.program)]
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)
        cls.source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        cls.header_sha256 = hashlib.sha256(header.read_bytes()).hexdigest()
        cls.harness_sha256 = hashlib.sha256(harness.encode()).hexdigest()
        cls.compiler_command = command

    def case(self, name):
        result = subprocess.run(
            [str(self.program), name], text=True, capture_output=True, timeout=15,
            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:halt_on_error=1",
                     UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1"),
        )
        RESULTS.append({"case": name, "exit_code": result.returncode,
                        "stdout": result.stdout, "stderr": result.stderr})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_enabled_features_success(self): self.case("features-success")
    def test_enabled_features_allocation_failure(self): self.case("features-nomem")
    def test_enabled_features_copy_from_user_failure(self): self.case("features-copyin")
    def test_enabled_features_copy_to_user_failure(self): self.case("features-copyout")
    def test_enabled_features_allocation_and_copyout_failure(self): self.case("features-nomem-copyout")
    def test_open_collision(self): self.case("open-collision")
    def test_open_device_key(self): self.case("open-device")
    def test_open_no_match(self): self.case("open-no-match")
    def test_open_uid_guards(self): self.case("open-uid")
    def test_open_filename_allocation_failure(self): self.case("open-nomem")
    def test_readlink_buffer_boundaries(self): self.case("readlink-boundaries")
    def test_readlink_copy_fault(self): self.case("readlink-copyout")
    def test_readlink_reverse_lookup_keys(self): self.case("readlink-keys")
    def test_proc_buffer_capacity(self): self.case("proc-boundaries")
    def test_proc_maximum_path(self): self.case("proc-maximum")
    def test_reverse_metadata_keys(self): self.case("metadata-keys")
    def test_reverse_metadata_no_match_and_invalid_output(self): self.case("metadata-no-match")
    def test_kstat_collision(self): self.case("kstat-collision")
    def test_kstat_device_key(self): self.case("kstat-device")
    def test_kstat_fuse_identity(self): self.case("kstat-fuse")
    def test_kstat_permission_and_inode_guards(self): self.case("kstat-guards")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path)
    args, remaining = parser.parse_known_args()
    SOURCE_ROOT = args.source_root.resolve()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(SusfsRuntimeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "source_root": str(SOURCE_ROOT),
            "source_sha256": getattr(SusfsRuntimeTests, "source_sha256", None),
            "header_sha256": getattr(SusfsRuntimeTests, "header_sha256", None),
            "harness_sha256": getattr(SusfsRuntimeTests, "harness_sha256", None),
            "compiler_command": getattr(SusfsRuntimeTests, "compiler_command", None),
            "extracted_functions": FUNCTIONS, "tests": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "passed": result.wasSuccessful(), "sanitizers": ["address", "undefined"],
            "scope": "Actual source functions, mocked kernel APIs; no device operations.",
            "cases": RESULTS,
        }, indent=2) + "\n")
    raise SystemExit(not result.wasSuccessful())
