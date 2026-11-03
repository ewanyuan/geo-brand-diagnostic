const token=document.querySelector('meta[name="login-token"]').content;
const portal=(()=>{try{return JSON.parse(document.getElementById('portal-config').textContent);}catch{return {shared:false};}})();
const state={platforms:JSON.parse(document.getElementById('platform-catalog').textContent),active:false,connected:false,preparing:false,selected:sessionStorage.getItem('geo-login-selected'),frame:null,busy:false,timer:null,viewTimer:null,liveTimer:null,liveRequest:null,serial:0};
const $=id=>document.getElementById(id);
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={not_prepared:'尚未准备',unknown:'等待连接',preparing:'准备中',login_ready:'等待登录',page_ready:'页面就绪 · 待确认',verification:'需人工验证',loading:'仍在加载',error:'需要重试'};
function notice(text,error=false){$('notice').textContent=text;$('notice').classList.toggle('error',error);}
async function call(payload){
 const response=await fetch('/api/login-center',{method:'POST',headers:{'Content-Type':'application/json','X-GEO-Login-Token':token},body:JSON.stringify(payload),cache:'no-store'});
 if(response.status===401&&portal.shared){$('screen').removeAttribute('src');location.replace('/');throw Error('本次登录已到期，请回首页重新进入');}
 const result=await response.json();if(!response.ok||result.error&&payload.action!=='status')throw Error(result.error||'操作未完成');return result;
}
function busy(value){state.busy=value;document.querySelectorAll('button').forEach(x=>x.disabled=value);$('prepare').disabled=value||state.preparing||state.active;$('retry').disabled=value||state.preparing;document.querySelectorAll('[data-site]').forEach(x=>x.disabled=value||state.preparing);$('finish').disabled=value||state.preparing||(!state.active&&!(portal.shared&&state.platforms.some(s=>s.confirmed)));['snapshot','reload','confirmed'].forEach(id=>$(id).disabled=value||!state.active||!state.selected||state.preparing);document.querySelectorAll('[data-key],[data-scroll]').forEach(x=>x.disabled=value||!state.frame);$('entry').disabled=value||!state.frame?.focus?.editable;$('type-submit').disabled=value||!state.frame?.focus?.editable;}
function renderCards(){
 $('platforms').innerHTML=state.platforms.map(s=>`<button class="platform ${s.id===state.selected?'selected':''}" data-site="${escape(s.id)}" ${state.busy?'disabled':''}><strong>${escape(s.name)}</strong><span class="badge ${s.confirmed?'good':['verification','error'].includes(s.state)?'warn':''}">${s.confirmed?'本人已确认登录':escape(labels[s.state]||s.state)}</span><small>${escape(s.methods?.join(' / ')||'查看登录页面')}</small><small>${portal.shared?'仅属于此入口的浏览器资料':s.collection?'已有网页采集入口':'已列入登录准备'}</small></button>`).join('');
 renderConsent();
}
function renderConsent(){
 if(!portal.shared)return;
 const enabled=Number(portal.collectionUntil||0)*1000>Date.now();
 $('collection-consent').hidden=!enabled;
 $('collection-consent-title').textContent='已交给 GEO，可关闭此页面';
 $('collection-consent-note').textContent=`你已允许 GEO 使用已确认的平台账号，有效至 ${new Date(Number(portal.collectionUntil||0)*1000).toLocaleString()}。点右侧按钮可立即撤销。`;
 $('collection-consent-toggle').textContent='停止 GEO 使用';
}
function showStatus(result){
 state.connected=result.connected;state.preparing=result.preparing;
 if(result.connected||!state.platforms.length){state.platforms=result.platforms||[];state.active=result.active;}
 if(!state.active||state.preparing)clearTimeout(state.liveTimer);
 renderCards();
 if(!result.connected)notice(result.error||'浏览器工作区暂时连接不上，稍后刷新状态即可。',true);
 else if(result.preparing)notice(portal.shared?'正在打开登录页，完成后会自动显示画面…':`正在${result.location||'专用浏览器'}依次准备六个平台。完成后会显示各平台状态。`);
 else if(result.active){const ready=state.platforms.filter(s=>['login_ready','page_ready','verification'].includes(s.state)).length;notice(portal.shared?'在下方实际网页中完成登录，再点「已登录，返回列表」。也可以继续点其他平台。':`${result.location||'专用浏览器'}：已打开 ${ready} / ${state.platforms.length} 个平台。选平台即可查看实际页面，方便时可集中登录。`);}
 else notice(portal.shared?(state.platforms.some(s=>s.confirmed)?'已保存你确认的平台。可点「完成，交给 GEO」更新使用授权，或点平台继续登录。':'点击下面要登录的平台即可开始。DeepSeek、豆包、Kimi 可交给 GEO 使用。'):'登录工作区尚未打开。点击“准备全部登录页”后，页面会独立保持。');
 clearTimeout(state.timer);if(result.preparing)state.timer=setTimeout(refreshStatus,8000);
}
async function refreshStatus(){if(state.busy){state.timer=setTimeout(refreshStatus,2000);return;}busy(true);try{showStatus(await call({action:'status'}));}catch(e){notice(e.message,true);}finally{busy(false);}if(state.active&&!state.preparing&&state.platforms.some(s=>s.id===state.selected&&(!portal.shared||s.opened))&&!state.frame)await action('snapshot');}
function showFrame(frame){
 state.frame=frame;const i=state.platforms.findIndex(s=>s.id===frame.site.id);if(i>=0)state.platforms[i]={...frame.site,opened:true};
 $('login-workspace').hidden=false;
 $('platform-title').textContent=frame.site.name;$('page-note').textContent=(frame.site.message||'')+' · 画面自动同步，扫码后请在手机上确认';
 $('screen').src='data:image/png;base64,'+frame.image;$('screen').hidden=false;$('placeholder').hidden=true;
 $('focus').textContent=frame.focus?.editable?`已选中网页输入框：${frame.focus.label||'可填写'}`:'先在画面中点击要填写的输入框，再在下面输入。';
 renderCards();
 scheduleLive();
}
function returnToPlatforms(site){
 clearTimeout(state.liveTimer);++state.serial;state.frame=null;state.selected=null;
 sessionStorage.removeItem('geo-login-selected');
 $('screen').hidden=true;$('screen').removeAttribute('src');$('login-workspace').hidden=true;
 renderCards();
 notice(`${site.name} 已由你确认登录。可以继续登录其他平台，或点「完成，交给 GEO」保存。`);
 $('platforms').scrollIntoView({behavior:'smooth',block:'start'});
}
function scheduleLive(delay=2500){
 clearTimeout(state.liveTimer);
 if(state.active&&!state.preparing&&state.selected&&state.frame)state.liveTimer=setTimeout(refreshLive,delay);
}
async function refreshLive(){
 if(!state.active||state.preparing||!state.frame||!state.selected)return;
 if(document.hidden||state.busy||state.liveRequest||document.activeElement===$('entry')||$('entry').value){scheduleLive();return;}
 const serial=state.serial,site=state.selected;
 state.liveRequest=(async()=>{
  try{
   const frame=await call({action:'snapshot',site});
   if(state.serial===serial&&state.selected===site&&state.active)showFrame(frame);
  }catch{
   if(state.serial===serial)$('page-note').textContent='画面同步暂时中断，正在重试；也可点「刷新画面」。';
  }
 })();
 try{await state.liveRequest;}finally{state.liveRequest=null;scheduleLive();}
}
async function action(name,extra={}){
 if(state.busy)return;
 if(state.liveRequest)await state.liveRequest;
 if(state.busy)return;
 clearTimeout(state.liveTimer);
 clearTimeout(state.viewTimer);const serial=++state.serial;
 busy(true);
 try{
  const result=await call({action:name,site:state.selected,frame_id:state.frame?.frame_id,...extra});
  if(result.image)showFrame(result);else showStatus(result);
  if(name==='confirmed'&&portal.shared&&result.image)returnToPlatforms(result.site);
  if(name==='finish'){$('screen').hidden=true;$('screen').removeAttribute('src');$('placeholder').hidden=false;$('placeholder').textContent='浏览器工作区已保存并关闭。';state.frame=null;notice(portal.shared?'你的专属浏览器工作区已保存并关闭。退出入口后，平台会话仍保存在服务器；需要继续时请在入口有效期内重新准备页面。':'浏览器工作区已保存并关闭。若已完成账号登录，可回诊断台运行豆包、Kimi 小批量采集核验。');}
 }catch(e){notice(e.message+(['click','key'].includes(name)?' 请先刷新画面确认网页结果，再决定下一步。':''),true);if(name==='snapshot')$('placeholder').textContent='尚未取得这个平台的画面，请刷新状态后重试。';}
 finally{busy(false);scheduleLive();}
 if(['click','key','refresh'].includes(name)&&state.active&&state.frame)scheduleLive(1200);
}
$('platforms').addEventListener('click',async e=>{const button=e.target.closest('[data-site]');if(!button||state.busy||state.preparing)return;clearTimeout(state.viewTimer);state.selected=button.dataset.site;sessionStorage.setItem('geo-login-selected',state.selected);state.frame=null;const site=state.platforms.find(s=>s.id===state.selected);renderCards();$('platform-title').textContent=site?.name||'';$('screen').hidden=true;$('placeholder').hidden=false;$('placeholder').textContent='正在打开这个平台…';if(portal.shared&&(!state.active||!site?.opened))await action('prepare');else if(state.active)await action('snapshot');else{notice('先点击“准备全部登录页”。');busy(false);}});
$('prepare').addEventListener('click',()=>action('prepare',{site:null}));
$('status').addEventListener('click',refreshStatus);
$('retry').addEventListener('click',()=>state.selected?action('prepare'):notice('先选择一个平台。'));
$('snapshot').addEventListener('click',()=>state.selected?action('snapshot'):notice('先选择一个平台。'));
$('reload').addEventListener('click',()=>state.selected?action('refresh'):notice('先选择一个平台。'));
$('confirmed').addEventListener('click',()=>state.selected?action('confirmed'):notice('先选择一个平台。'));
$('finish').addEventListener('click',()=>portal.shared?completeLogin():action('finish'));
async function completeLogin(){
 if(state.busy)return;
 if(state.liveRequest)await state.liveRequest;
 if(state.busy)return;
 const confirmed=state.platforms.filter(s=>s.confirmed);
 if(!confirmed.length){notice('登录后先点「已登录，返回列表」，再点「完成，交给 GEO」。',true);return;}
 clearTimeout(state.liveTimer);++state.serial;busy(true);
 try{
  const response=await fetch('/api/access/complete',{method:'POST',headers:{'Content-Type':'application/json','X-GEO-Login-Token':token},body:JSON.stringify({allow_collection:confirmed.some(s=>['deepseek','doubao','kimi'].includes(s.id))}),cache:'no-store'});
  const result=await response.json();if(!response.ok)throw Error(result.error||'保存未完成，请重试');
  portal.collectionUntil=result.until;showStatus(result.workspace);state.frame=null;
  $('screen').hidden=true;$('screen').removeAttribute('src');$('placeholder').hidden=false;
  $('placeholder').textContent=result.enabled?'已完成，可以关闭此页面。':'登录会话已保存。';
  notice(result.enabled?'已完成！GEO 现在可以使用你已确认的平台账号；你可以关闭此页面。':'登录会话已保存。当前网页采集支持 DeepSeek、豆包、Kimi，其余平台会话先保留。');
  renderConsent();
 }catch(e){notice(e.message,true);}
 finally{busy(false);scheduleLive();}
}
$('screen').addEventListener('click',e=>{if(!state.frame||state.busy)return;const r=e.currentTarget.getBoundingClientRect();action('click',{x:Math.floor((e.clientX-r.left)*state.frame.width/r.width),y:Math.floor((e.clientY-r.top)*state.frame.height/r.height)});});
$('type-form').addEventListener('submit',e=>{e.preventDefault();const text=$('entry').value;$('entry').value='';if(text)action('type',{text});});
document.querySelectorAll('[data-key]').forEach(b=>b.addEventListener('click',()=>action('key',{key:b.dataset.key})));
document.querySelectorAll('[data-scroll]').forEach(b=>b.addEventListener('click',()=>action('scroll',{dy:Number(b.dataset.scroll)})));
if(portal.shared){
 document.title='专属平台登录中心';$('portal-brand').querySelector('span').textContent='平台登录中心';$('portal-brand').href='/login-center';$('portal-home').hidden=true;$('logout').hidden=false;
 $('portal-description').textContent=`${portal.name}，点下面的平台登录。DeepSeek、豆包、Kimi 可交给 GEO 提问。`;
 $('portal-location').textContent='你的登录页面';
 document.querySelector('h1').textContent='选个平台，登录就好';
 $('setup-title').textContent='① 点平台　② 登录后点「已登录，返回列表」　③ 完成，交给 GEO';
 $('setup-description').textContent='点「完成，交给 GEO」会保存会话，并允许运营者最长 12 小时使用已确认的 DeepSeek、豆包、Kimi 账号提问。你可随时停止授权。';
 $('prepare').hidden=true;$('finish').textContent='完成，交给 GEO';$('finish').classList.add('primary');$('confirmed').textContent='已登录，返回列表';$('login-workspace').hidden=true;
 $('placeholder').textContent='先点上方一个平台，登录页面会显示在这里。';
 $('portal-privacy').textContent='本页显示服务器上的浏览器。输入内容只用于填写你选中的平台，不写入操作日志。平台登录会话会保存在服务器，由服务运营方管理；其他参与者不能通过自己的入口查看或操作你的工作区。请仅在信任运营方并了解用途后登录。';
 $('portal-footer').textContent='完成后可直接关闭页面；点“退出此入口”会停止 GEO 使用，但平台会话仍保留。登录确认由你本人操作，实际采集结果仍需核验。';
 renderConsent();
 $('collection-consent-toggle').addEventListener('click',async()=>{
  const enabled=!(Number(portal.collectionUntil||0)*1000>Date.now());
  if(enabled&&!window.confirm('授权后，登录中心运营者可在最长 12 小时内，通过 GEO 使用你已确认的 DeepSeek、豆包或 Kimi 账号发送监测问题并取得回答。授权不会超过当前入口有效期，退出入口时会自动撤销。确认授权吗？'))return;
  $('collection-consent-toggle').disabled=true;
  try{
   const response=await fetch('/api/access/collection-consent',{method:'POST',headers:{'Content-Type':'application/json','X-GEO-Login-Token':token},body:JSON.stringify({enabled}),cache:'no-store'});
   const result=await response.json();if(!response.ok)throw Error(result.error||'授权状态更新失败');
   portal.collectionUntil=result.until;renderConsent();notice(enabled?'已授权 GEO 在授权时限内使用你已确认的平台登录资料。':'已撤销授权。');
  }catch(e){notice(e.message,true);}
  finally{$('collection-consent-toggle').disabled=false;}
 });
 $('logout').addEventListener('click',async()=>{if(state.busy)return;if(Number(portal.collectionUntil||0)*1000>Date.now()&&!window.confirm('退出此入口会立即撤销 GEO 使用此账号的权限，平台会话仍保留。要继续吗？'))return;busy(true);try{const r=await fetch('/api/access/logout',{method:'POST',headers:{'X-GEO-Login-Token':token}});if(!r.ok)throw Error('退出未完成，请重试');$('screen').removeAttribute('src');location.replace('/');}catch(e){notice(e.message,true);busy(false);}});
 window.addEventListener('pagehide',()=>{clearTimeout(state.liveTimer);++state.serial;$('screen').removeAttribute('src');$('entry').value='';});
 window.addEventListener('pageshow',e=>{if(e.persisted)location.reload();});
}
renderCards();refreshStatus();
