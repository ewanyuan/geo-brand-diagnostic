"""Local browser regressions. Website fixture replies are never real GEO samples."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from local_login_service import Workspace
from browser_discovery import browser_candidates


CHAT_PAGE = b'''<!doctype html><meta charset="utf-8"><body>
<div data-virtual-list-item-key="old"><div class="ds-markdown">OLD ANSWER MUST NOT BE COLLECTED</div><button aria-label="Read aloud"></button></div>
<textarea placeholder="Message DeepSeek"></textarea><div id="messages"></div>
<button id="stop" hidden><svg></svg></button>
<script>
const editor=document.querySelector('textarea');
editor.addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;e.preventDefault();
 const text=editor.value;editor.value='';
 const prompt=document.createElement('p');prompt.textContent=text;messages.append(prompt);
 const row=document.createElement('div');row.dataset.virtualListItemKey='new';messages.append(row);
 const answer=document.createElement('div');answer.className='ds-markdown';row.append(answer);
 const stop=document.querySelector('#stop');stop.hidden=false;
 answer.textContent='PARTIAL';
 setTimeout(()=>{answer.textContent='FIXTURE REPLY: '+text;const read=document.createElement('button');read.setAttribute('aria-label','Read aloud');row.append(read);stop.hidden=true;},text.includes('pause')?11000:1000);
});
</script>'''
LOGIN_PAGE = b'<body><input placeholder="Phone number"><button>Send code</button><p>Scan with Wechat</p></body>'
KIMI_PAGE = b'''<body><textarea hidden>OLD HIDDEN DRAFT</textarea>
<div style="opacity:0"><input placeholder="Phone number"><div role="dialog">Too many requests</div></div>
<div class="chat-input-editor" contenteditable="true" role="textbox"></div><main></main>
<script>const box=document.querySelector('[contenteditable]');box.addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;e.preventDefault();const text=box.innerText;box.innerText='';
 const prompt=document.createElement('p');prompt.className='segment-user';prompt.textContent=text;document.querySelector('main').append(prompt);
 const row=document.createElement('div');row.className='segment-assistant';
 row.innerHTML='<div class="markdown-container toolcall-content-text"><div class="markdown">THINKING MUST NOT BE COLLECTED</div></div><div class="markdown-container"><div class="markdown" aria-label="CHAT_TEXT_SENTINEL">FINAL KIMI FIXTURE</div></div><div class="segment-assistant-actions" style="width:100px;height:20px"><svg></svg><svg></svg><svg></svg></div>';
 document.querySelector('main').append(row);
});</script></body>'''
BUSY_PAGE = b'''<body><textarea placeholder="Message DeepSeek"></textarea>
<script>const box=document.querySelector('textarea');box.addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;e.preventDefault();
 const modal=document.createElement('div');modal.setAttribute('role','dialog');modal.textContent='Too many requests';document.body.append(modal);
});</script></body>'''
DOUBAO_PAGE = b'''<body><textarea placeholder="&#21457;&#28040;&#24687;" data-testid="chat_input"></textarea><main></main>
<script>const box=document.querySelector('textarea');box.addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;e.preventDefault();const text=box.value;box.value='';
 const prompt=document.createElement('p');prompt.dataset.testid='send_message';prompt.textContent=text;document.querySelector('main').append(prompt);
 const row=document.createElement('div');row.dataset.testid='receive_message';
 const answer=document.createElement('div');answer.dataset.testid='message_text_content';answer.dataset.streaming='true';answer.textContent='PARTIAL';row.append(answer);document.querySelector('main').append(row);
 setTimeout(()=>{answer.textContent='FINAL DOUBAO FIXTURE: '+text;answer.dataset.streaming='false';},1000);
});</script></body>'''


class FixtureWorkspace(Workspace):
    def start(self):
        if self.context:
            return
        self.engine = sync_playwright().start()
        self.context = self.engine.chromium.launch_persistent_context(
            str(self.root / 'browser-profile'), headless=True,
            **browser_candidates('chromium' if sys.platform.startswith('linux') else 'chrome')[0][1])
        self.context.set_default_timeout(3000)
        self.context.set_default_navigation_timeout(10000)


class LocalBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                virtual = DOUBAO_PAGE.replace(b"const row=document.createElement('div');", b"prompt.remove();const row=document.createElement('div');")
                auth = b"const login=document.createElement('input');login.placeholder='Phone number';document.body.append(login);"
                transient = DOUBAO_PAGE.replace(b"const row=document.createElement('div');", auth+b"setTimeout(()=>login.remove(),1100);const row=document.createElement('div');")
                persistent = DOUBAO_PAGE.replace(b"const row=document.createElement('div');", auth+b"const row=document.createElement('div');")
                data = {'/login':LOGIN_PAGE,'/kimi':KIMI_PAGE,'/busy':BUSY_PAGE,'/doubao':DOUBAO_PAGE,'/doubao-virtual':virtual,
                        '/auth-transition':transient,'/auth-required':persistent}.get(self.path,CHAT_PAGE)
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(data)
        cls.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='geo-local-fixture-')
        self.workspace = FixtureWorkspace(root=self.temp.name)

    def tearDown(self):
        self.workspace.request({'action':'shutdown'})
        self.workspace.thread.join(timeout=5)
        self.temp.cleanup()

    def url(self, path):
        return f'http://127.0.0.1:{self.http.server_port}{path}'

    def test_A5_three_platforms_through_product_keep_original_answer_and_evidence(self):
        from server import create_app
        import io, zipfile
        root = Path(self.temp.name)
        app = create_app({'TESTING':True, 'DATABASE':str(root/'fixture.sqlite3'),
                          'EVIDENCE_DIR':str(root/'evidence'), 'SEED_DEMO':False})
        client = app.test_client()
        pid = client.post('/api/projects', json={'brand':'FIXTURE BRAND', 'category':'FIXTURE CATEGORY'}).json['id']
        qid = client.get(f'/api/projects/{pid}').json['questions'][0]['id']
        sites = {'deepseek':{'url':self.url('/chat')}, 'doubao':{'url':self.url('/doubao')}, 'kimi':{'url':self.url('/kimi')}}
        def collect(platform, questions, participant_id):
            return self.workspace.request({'action':'collect','platform':platform,'questions':questions})['results']
        with patch.dict('local_login_service.CATALOG',sites), patch('server.is_local',return_value=True), patch('server.browser_ready',return_value=True), patch('server.collect_remote',side_effect=collect):
            for platform in ('DeepSeek','豆包','Kimi'):
                response = client.post(f'/api/projects/{pid}/run-browser',json={'platform':platform,'question_ids':[qid]})
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json['results'][0]['status'],'complete',response.json)
        rows = client.get(f'/api/projects/{pid}/observations').json
        self.assertEqual({r['platform'] for r in rows},{'DeepSeek','豆包','Kimi'})
        for row in rows:
            self.assertIn('FIXTURE',row['answer'])
            self.assertTrue(row['asked_at'])
            self.assertTrue(row['page_url'].startswith(self.url('/')))
            self.assertTrue((root/'evidence'/row['evidence_image']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        exported = client.get(f'/api/projects/{pid}/export.zip')
        with zipfile.ZipFile(io.BytesIO(exported.data)) as archive:
            self.assertEqual(len([n for n in archive.namelist() if n.startswith('evidence/')]),3)

    def test_A5_sent_user_message_can_disappear_before_enter_returns(self):
        with patch.dict('local_login_service.CATALOG',{'doubao':{'url':self.url('/doubao-virtual')}}):
            result=self.workspace.request({'action':'collect','platform':'豆包',
                'questions':[{'id':1,'text':'fixture user bubble immediately virtualized'}]})
        row=result['results'][0]
        self.assertEqual(row['status'],'complete',row.get('error_message'))
        self.assertEqual(row['answer'],'FINAL DOUBAO FIXTURE: fixture user bubble immediately virtualized')
        self.assertTrue(row['asked_at'])

    def test_A5_transient_auth_hydration_differs_from_a_persistent_login_request(self):
        with patch.dict('local_login_service.CATALOG',{'doubao':{'url':self.url('/auth-transition')}}):
            result=self.workspace.request({'action':'collect','platform':'豆包','questions':[{'id':1,'text':'fixture auth transition'}]})
        self.assertEqual(result['results'][0]['status'],'complete',result)
        with patch.dict('local_login_service.CATALOG',{'doubao':{'url':self.url('/auth-required')}}):
            result=self.workspace.request({'action':'collect','platform':'豆包','questions':[{'id':2,'text':'fixture login is required'}]})
        self.assertEqual(result['results'][0]['status'],'error')
        self.assertIn('要求登录',result['results'][0]['error_message'])
        self.assertFalse(result['results'][0]['asked_at'])

    def test_A5_remounted_old_message_does_not_confirm_an_unsent_draft(self):
        # This direct helper check owns and closes its browser on this thread;
        # the Workspace worker is not used to create/close this context.
        with sync_playwright() as engine:
            browser=engine.chromium.launch(headless=True,
                **browser_candidates('chromium' if sys.platform.startswith('linux') else 'chrome')[0][1])
            try:
                page=browser.new_page()
                page.set_content('<textarea></textarea><p data-testid="send_message">fixture repeated question</p>')
                editor=page.locator('textarea')
                editor.fill('fixture repeated question')
                handle=editor.element_handle()
                key=self.workspace.watch_sent(page,'doubao','fixture repeated question')
                page.evaluate("()=>{const old=document.querySelector('[data-testid=send_message]');old.replaceWith(old.cloneNode(true))}")
                with self.assertRaisesRegex(ValueError,'未确认问题已发送'):
                    self.workspace.confirm_sent(page,handle,'doubao','fixture repeated question',key,timeout=1)
                self.assertEqual(editor.input_value(),'fixture repeated question')
            finally:
                browser.close()

    def test_new_answers_keep_evidence_and_reuse_open_browser(self):
        with patch.dict('local_login_service.CATALOG', {'deepseek':{'url':self.url('/chat')}}):
            self.workspace.request({'action':'open','site':'deepseek'})
            result = self.workspace.request({'action':'collect','platform':'DeepSeek',
                'questions':[{'id':1,'text':'fixture question one'},{'id':2,'text':'fixture question two with pause and 手机号登录'}]})
        rows = result['results']
        self.assertEqual([r['answer'] for r in rows], ['FIXTURE REPLY: fixture question one','FIXTURE REPLY: fixture question two with pause and 手机号登录'])
        for row in rows:
            self.assertEqual(row['status'], 'complete')
            self.assertTrue(row['asked_at'])
            self.assertTrue(row['screenshot_base64'].startswith('iVBOR'))
        self.assertTrue(self.workspace.status()['active'])
        saved = json.loads((Path(self.temp.name)/'login-readiness.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['platforms'][0]['last_answer_at'])

    def test_login_blocks_send_and_stops_rest_of_batch(self):
        with patch.dict('local_login_service.CATALOG', {'deepseek':{'url':self.url('/login')}}):
            result = self.workspace.request({'action':'collect','platform':'DeepSeek',
                'questions':[{'id':1,'text':'do not send'},{'id':2,'text':'do not send either'}]})
        rows = result['results']
        self.assertEqual([r['status'] for r in rows], ['error','error'])
        self.assertFalse(rows[0]['asked_at'])
        self.assertNotIn('screenshot_base64', rows[0])
        self.assertIn('登录', rows[0]['error_message'])
        self.assertIn('尚未发送', rows[1]['error_message'])

    def test_kimi_sends_visible_editor_while_hidden_draft_stays_untouched(self):
        with patch.dict('local_login_service.CATALOG', {'kimi':{'url':self.url('/kimi')}}):
            result = self.workspace.request({'action':'collect','platform':'Kimi',
                'questions':[{'id':1,'text':'fixture new question'}]})
        self.assertEqual(result['results'][0]['status'],'complete',result['results'][0].get('error_message'))
        self.assertEqual(result['results'][0]['answer'],'FINAL KIMI FIXTURE')
        meta=self.workspace.request({'action':'inspect_chat','site':'kimi'})
        encoded=json.dumps(meta)
        self.assertNotIn('OLD HIDDEN DRAFT',encoded)
        self.assertNotIn('CHAT_TEXT_SENTINEL',encoded)
        self.assertNotIn('FINAL KIMI FIXTURE',encoded)

    def test_platform_busy_dialog_stops_without_waiting_for_answer_timeout(self):
        with patch.dict('local_login_service.CATALOG', {'deepseek':{'url':self.url('/busy')}}):
            result = self.workspace.request({'action':'collect','platform':'DeepSeek',
                'questions':[{'id':1,'text':'fixture new question'}]})
        row=result['results'][0]
        self.assertEqual(row['status'],'error')
        self.assertFalse(row['answer'])
        self.assertIn('平台当前繁忙',row['error_message'])


if __name__ == '__main__':
    unittest.main()
