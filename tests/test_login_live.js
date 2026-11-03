// Exercise the shipped login script with a phone-side page change and async responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
assert.match(fs.readFileSync(path.join(__dirname,'../static/login.html'),'utf8'),/id="login-workspace"/);

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    id, value:'', textContent:'', hidden:false, disabled:false, listeners:{},
    classList:{add(){},toggle(){}},
    addEventListener(name, fn){this.listeners[name]=fn;},
    removeAttribute(name){delete this[name];},
    scrollIntoView(){},
    querySelector(){return element(id+'-span');},
  });
  return elements.get(id);
}
const site = {id:'doubao',name:'Doubao',state:'login_ready',message:'Waiting',opened:true,confirmed:false};
element('platform-catalog').textContent = JSON.stringify([site]);
element('portal-config').textContent = JSON.stringify({shared:true,name:'Test',collectionUntil:0});
element('meta').content = 'test-csrf';
const timers = new Map();
let timerId = 0, snapshots = 0, mobileConfirmed = false, holdNext = false, releaseSnapshot;
const requests=[];
const frame = id => ({frame_id:id,site:{...site,state:mobileConfirmed?'page_ready':'login_ready'},
  image:mobileConfirmed?'after-mobile-confirmation':'qr-page',width:1000,height:700,focus:{editable:false}});
const context = vm.createContext({
  console, Date,
  document:{hidden:false,activeElement:null,getElementById:element,
    querySelector:s=>s.startsWith('meta')?element('meta'):element('heading'),
    querySelectorAll:s=>s==='button'?['prepare','retry','finish','confirmed','snapshot','status','reload'].map(element):[]},
  sessionStorage:{getItem:()=> 'doubao',setItem(){},removeItem(){}},
  window:{addEventListener(){},confirm:()=>true},location:{replace(){}},
  setTimeout(fn, delay){const id=++timerId;timers.set(id,{fn,delay});return id;},
  clearTimeout(id){timers.delete(id);},
  async fetch(url, options){
    const payload=JSON.parse(options.body);requests.push(payload);
    let result;
    if(payload.action==='status')result={connected:true,active:true,preparing:false,platforms:[site]};
    else if(payload.action==='snapshot'){
      const id='frame-'+(++snapshots);
      if(holdNext){holdNext=false;await new Promise(resolve=>{releaseSnapshot=resolve;});}
      result=frame(id);
    }else{if(payload.action==='confirmed')site.confirmed=true;result=frame('after-action');}
    return {ok:true,status:200,json:async()=>result};
  },
});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function nextTimer(){
  const [id,timer]=[...timers.entries()].sort((a,b)=>a[1].delay-b[1].delay)[0];
  timers.delete(id);return timer.fn();
}

async function main(){
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/login.js'),'utf8'),context);
  await flush();
  assert.equal(element('screen').src,'data:image/png;base64,qr-page');
  mobileConfirmed=true;
  await nextTimer();
  assert.equal(element('screen').src,'data:image/png;base64,after-mobile-confirmation');
  assert.equal(vm.runInContext('state.platforms[0].confirmed',context),false,'screen refresh must not grant login confirmation');

  const beforeTyping=snapshots;
  context.document.activeElement=element('entry');
  element('entry').value='local input';
  await nextTimer();
  assert.equal(snapshots,beforeTyping,'typing must not trigger a background capture');
  assert.equal(element('entry').value,'local input');
  context.document.activeElement=null;element('entry').value='';
  await nextTimer();assert.equal(snapshots,beforeTyping+1,'refresh must resume after typing');

  context.document.hidden=true;
  const beforeHidden=snapshots;await nextTimer();assert.equal(snapshots,beforeHidden);
  context.document.hidden=false;await nextTimer();assert.equal(snapshots,beforeHidden+1);

  holdNext=true;const live=nextTimer();await flush();
  const currentFrame='frame-'+snapshots;
  const action=vm.runInContext("action('key',{key:'ENTER'})",context);
  await flush();assert.equal(requests.filter(r=>r.action==='key').length,0);
  releaseSnapshot();await live;await action;
  assert.equal(requests.find(r=>r.action==='key').frame_id,currentFrame,'input must wait for the current frame token');

  holdNext=true;const stale=nextTimer();await flush();
  vm.runInContext("state.selected='kimi';state.frame=null;state.serial++",context);
  releaseSnapshot();await stale;
  assert.equal(vm.runInContext('state.frame',context),null,'old platform response must not restore its image');
  vm.runInContext("state.selected='doubao';state.frame={frame_id:'before-confirmation'}",context);
  await vm.runInContext("action('confirmed')",context);
  assert.equal(element('login-workspace').hidden,true);
  assert.equal(vm.runInContext('state.selected',context),null);
  assert.equal(vm.runInContext('state.platforms[0].confirmed',context),true);
  assert.equal(timers.size,0,'returning to the list must stop screenshot capture');
  vm.runInContext('state.active=false;busy(false)',context);
  assert.equal(element('finish').disabled,false,'saved confirmed profiles must allow renewed consent without opening the browser');
  console.log('Live login view: 8 regression checks passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
