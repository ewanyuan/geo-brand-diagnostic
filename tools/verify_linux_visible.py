"""Production Workspace in an isolated Linux display; no real accounts or sites."""

import argparse
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_login_service import Workspace


def verify(output):
    output.mkdir(parents=True, exist_ok=True)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            body = '<!doctype html><meta charset="utf-8"><h1>GEO 程序验收，非真实登录</h1><label>输入检查<input id="input"></label>'.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{http.server_port}'
    display_log = open(output/'linux-display.log', 'w')
    display = None
    workspace = None
    profile = None
    previous_display = os.environ.get('DISPLAY')
    report = {'virtual_display': True, 'physical_desktop_verified': False,
              'real_platform_accounts_verified': False, 'uses_production_workspace_start': True,
              'runtime': 'source', 'packaged': False, 'passed': False}
    try:
        display = subprocess.Popen(['Xvfb', '-displayfd', '1', '-screen', '0', '1440x1000x24'],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=display_log)
        ready, _, _ = select.select([display.stdout], [], [], 10)
        if not ready or display.poll() is not None:
            raise RuntimeError('Isolated display did not start')
        number = display.stdout.readline().decode('ascii').strip()
        if not number.isdecimal() or len(number)>4 or display.poll() is not None:
            raise RuntimeError('Invalid display allocation')
        os.environ['DISPLAY'] = ':'+number
        profile = Path(tempfile.mkdtemp(prefix='geo-linux-visible-'))
        workspace = Workspace(root=profile, runtime_config={})
        original = workspace.perform
        def perform(payload):
            if payload['action'] != 'fixture':
                return original(payload)
            workspace.start()
            page = workspace.context.new_page()
            page.goto(origin, wait_until='domcontentloaded')
            page.locator('#input').fill('GEO 中文输入验收')
            assert page.locator('#input').input_value() == 'GEO 中文输入验收'
            if payload['phase'] == 'save':
                workspace.context.add_cookies([{'name':'geo_fixture_session','value':'TEST_ONLY',
                    'url':origin,'expires':time.time()+3600}])
                page.screenshot(path=str(output/'linux-native-input-fixture.png'))
            else:
                assert any(c['name']=='geo_fixture_session' and c['value']=='TEST_ONLY'
                           for c in workspace.context.cookies([origin]))
            return {'input_verified': True, 'browser':workspace.browser_channel}
        workspace.perform = perform
        first = workspace.request({'action':'fixture','phase':'save'})
        assert first.get('input_verified'), first
        assert not workspace.request({'action':'finish'}).get('error')
        second = workspace.request({'action':'fixture','phase':'reopen'})
        assert second.get('input_verified'), second
        report.update(native_window_input_verified=True, persistent_profile_after_reopen_verified=True,
                      browser=second['browser'])
        report['passed'] = True
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_error = False
        if workspace:
            try:
                closed = workspace.request({'action':'shutdown'})
                workspace.thread.join(timeout=10)
                cleanup_error = bool(closed.get('error') or workspace.thread.is_alive())
            except Exception:
                cleanup_error = True
        # Never remove a profile before its owning Playwright thread has exited.
        if profile and not cleanup_error:
            shutil.rmtree(profile)
        if display:
            if display.poll() is None:
                display.terminate()
            display.wait(timeout=10)
            display.stdout.close()
        display_log.close()
        http.shutdown(); http.server_close()
        if previous_display is None:
            os.environ.pop('DISPLAY', None)
        else:
            os.environ['DISPLAY'] = previous_display
        if cleanup_error:
            report.update(passed=False, cleanup_error=True, fixture_profile_retained=True)
        (output/'linux-visible-browser-check.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        if cleanup_error and not active_error:
            raise RuntimeError('Browser worker cleanup failed; fixture profile was retained')
    return report


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    print(json.dumps(verify(parser.parse_args().output.resolve()),ensure_ascii=True))
