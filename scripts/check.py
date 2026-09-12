"""Run isolated LocalPilot regressions. No ChatGPT/model API calls or user browser sessions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
CORE = ['local', 'harness', 'local_skills', 'upgrades', 'image_workflow',
        'review_fixes', 'regressions', 'audit_edges', 'image_bridge_edges',
        'images', 'image_edit', 'image_handoff', 'long_runtime']
BROWSER = ['browser', 'browser_sessions', 'capability_fixes', 'panel']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser', action='store_true', help='Also test isolated Chrome and the panel')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('The complete regression suite currently requires macOS.')
    out = ROOT / 'verification/latest'
    out.mkdir(parents=True, exist_ok=True)

    def run(name):
        report = out / (name + '.json')
        command = [sys.executable, str(ROOT / 'scripts' / f'verify_{name}.py')]
        if name == 'panel':
            command += [str(out / 'screenshots'), str(report)]
        else:
            command += ['--output', str(report)]
        if name == 'long_runtime':
            command += ['--edges']  # No hour-long soak by default.
        report.unlink(missing_ok=True)
        started = time.monotonic()
        with (out / (name + '.log')).open('w') as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        data = json.loads(report.read_text()) if report.exists() else {}
        row = {'suite': name, 'exit_code': result.returncode, 'passed': data.get('passed', 0),
               'total': data.get('total', 0), 'seconds': round(time.monotonic() - started, 2)}
        row['ok'] = result.returncode == 0 and bool(row['total']) and row['passed'] == row['total']
        print(f'{"PASS" if row["ok"] else "FAIL"} {name}: {row["passed"]}/{row["total"]}', flush=True)
        return row

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(run, CORE + (BROWSER if args.browser else [])))
    summary = {'created_at': datetime.now(timezone.utc).isoformat(), 'suites': rows,
               'passed': sum(r['passed'] for r in rows), 'total': sum(r['total'] for r in rows),
               'ok': all(r['ok'] for r in rows), 'includes_browser': args.browser}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(f'Total: {summary["passed"]}/{summary["total"]}; verification/latest/summary.json')
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
