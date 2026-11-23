#!/usr/bin/env python3
"""Run the candidate's host regressions without operating a phone."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=ROOT / 'reports/regression-suite.json')
    parser.add_argument('--phase', choices=('all', 'pre', 'post'), default='all',
                        help='Run output-independent tests before build or generated-output tests after build')
    args = parser.parse_args()
    args.report = args.report.resolve()
    logs = args.report.parent / (args.report.stem + '-logs')
    logs.mkdir(parents=True, exist_ok=True)
    commands = [
        ('vfs', [sys.executable, str(ROOT / 'tests/test_vfs_redirect.py')]),
        ('susfs', [sys.executable, str(ROOT / 'tests/test_susfs_runtime.py')]),
        ('cpu-spoof', [sys.executable, str(ROOT / 'tests/test_cpu_spoof.py')]),
        ('uapi', [sys.executable, str(ROOT / 'tests/test_pipeline_uapi.py')]),
        ('provenance', [sys.executable, str(ROOT / 'tests/test_pipeline_provenance.py')]),
        ('readonly-delivery', [sys.executable, str(ROOT / 'tests/test_pipeline_readonly_verify.py')]),
        ('launcher', [sys.executable, str(ROOT / 'tests/test_pipeline_launcher.py')]),
        ('module', ['node', '--test', str(ROOT / 'tests/module/module.test.cjs')]),
        ('delivery', [sys.executable, str(ROOT / 'scripts/test-delivery.py')]),
        ('build-gates', [sys.executable, str(ROOT / 'scripts/test-build-gates.py')]),
        ('hook-contracts', [sys.executable, str(ROOT / 'scripts/test-hook-contracts.py')]),
    ]
    post_build = {'build-gates', 'hook-contracts'}
    if args.phase != 'all':
        commands = [(name, command) for name, command in commands
                    if (name in post_build) == (args.phase == 'post')]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for key in ('PYTHONOPTIMIZE', 'FUXI_TEST_SOURCE_ROOT', 'SUSFS_MODULE_ROOT'):
        environment.pop(key, None)
    results = []
    for name, command in commands:
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=ROOT, env=environment, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=300)
            output, code = result.stdout, result.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            output, code = str(error), 1
        log = logs / (name + '.log')
        log.write_text(output)
        count = re.search(r'Ran (\d+) tests?', output)
        if name == 'module':
            count = re.search(r'^# tests (\d+)$', output, re.M)
        record = {'name': name, 'command': command, 'exit_code': code,
                  'passed': code == 0, 'seconds': round(time.monotonic() - started, 2),
                  'log': str(log.relative_to(args.report.parent)),
                  'unit_test_groups': int(count.group(1)) if count else None}
        results.append(record)
        print(f'{name}: {"PASS" if code == 0 else "FAIL"} ({record["seconds"]}s)', flush=True)
    report = {'completed_at': datetime.now(timezone.utc).isoformat(),
              'source_root': str(ROOT), 'passed': all(x['passed'] for x in results),
              'phase': args.phase, 'device_operations': False, 'results': results}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
