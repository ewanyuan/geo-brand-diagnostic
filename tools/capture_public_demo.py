"""Render only the built-in fictional demo, using an isolated temporary DB."""

import argparse
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import create_app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def capture(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='geo-public-demo-') as temporary:
        root=Path(temporary)
        app=create_app({'DATABASE':str(root/'demo.sqlite3'),'EVIDENCE_DIR':str(root/'evidence'),'SEED_DEMO':True})
        http=make_server('127.0.0.1',0,app,threaded=True)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as engine:
                browser=engine.chromium.launch(channel='chrome',headless=True)
                try:
                    page=browser.new_page(viewport={'width':1440,'height':1050},device_scale_factor=1)
                    page.goto(f'http://127.0.0.1:{http.server_port}/',wait_until='networkidle')
                    page.get_by_text('演示数据',exact=False).first.wait_for()
                    page.screenshot(path=str(output))
                finally:
                    browser.close()
        finally:
            http.shutdown();thread.join(timeout=10);http.server_close()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    capture(parser.parse_args().output.resolve())
