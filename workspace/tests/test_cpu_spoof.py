#!/usr/bin/env python3
"""Exercise the actual SET_SPOOF_CPU handler with host stand-ins for kernel data.

On arm64, loops_per_jiffy calibrates udelay()/ndelay() and elf_hwcap supplies
AT_HWCAP/AT_HWCAP2, so the handler may change only the reported MIDR. The
source-root option runs the same assertions against another assembled tree.
This is a unit harness, not a device or full-kernel test.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = ROOT


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


def harness_source(source_root):
    driver = source_root / "kernel/KernelSU/kernel"
    function = extract_block((driver / "feature/cpu_spoof.c").read_text(),
                             r"^int ksu_set_spoof_cpu\(const struct ksu_set_spoof_cpu_cmd \*cmd\)\s*\{")
    structure = extract_block((driver / "include/uapi/supercall.h").read_text(),
                              r"^struct ksu_set_spoof_cpu_cmd\s*\{", ";")
    harness = (ROOT / "tests/cpu_spoof_harness.c").read_text()
    return (harness.replace("/* SOURCE_STRUCTURES */", structure)
                   .replace("/* SOURCE_FUNCTIONS */", function))


class CpuSpoofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="cpu-spoof-unit-")
        cls.addClassCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        (folder / "cpu_spoof.c").write_text(harness_source(SOURCE_ROOT))
        cls.program = folder / "cpu_spoof"
        compiler = shutil.which("cc")
        if compiler is None:
            raise RuntimeError("Host C compiler required for CPU spoof unit tests")
        command = [compiler, "-std=gnu11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
                   "-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                   "-fno-pie", "-no-pie", str(folder / "cpu_spoof.c"), "-o", str(cls.program)]
        compiled = subprocess.run(command, text=True, capture_output=True, timeout=60)
        if compiled.returncode:
            raise RuntimeError(compiled.stdout + compiled.stderr)

    def case(self, name):
        result = subprocess.run(
            [str(self.program), name], text=True, capture_output=True, timeout=15,
            env=dict(os.environ, ASAN_OPTIONS="detect_leaks=1:halt_on_error=1",
                     UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_midr_only_request_changes_only_that_cpu(self): self.case("midr-only")
    def test_secondary_cpu_midr(self): self.case("secondary-cpu")
    def test_bogomips_is_rejected_before_any_change(self): self.case("manager-prefill")
    def test_hwcap_is_rejected_before_any_change(self): self.case("hwcap")
    def test_invalid_cpu_and_null_request(self): self.case("cpu-bounds")
    def test_unresolved_cpu_data_changes_nothing(self): self.case("lookup-failure")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    args, remaining = parser.parse_known_args()
    SOURCE_ROOT = args.source_root.resolve()
    unittest.main(argv=[sys.argv[0], *remaining])
