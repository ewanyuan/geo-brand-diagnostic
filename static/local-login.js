const token = document.querySelector('meta[name="login-token"]').content;
const platforms = document.getElementById("platforms");
const notice = document.getElementById("notice");
let busy = false;
let latest = null;
function esc(value) { return String(value ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }
async function action(payload) {
  const response = await fetch("/api/login-center", {method:"POST", headers:{"Content-Type":"application/json","X-GEO-Login-Token":token}, body:JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok || data.error) throw Error(data.error || "浏览器未响应");
  return data;
}
function render(data) {
  latest = data;
  const working = data.collecting || data.preparing;
  document.getElementById("finish").disabled = working || !data.active;
  notice.className = "";
  notice.textContent = data.collecting ? "GEO 正在浏览器中提问，请等当前批次结束。" : data.active ? "专用浏览器已打开。请直接在浏览器窗口操作，登录后可回诊断台采集。" : "点击下方平台打开浏览器。以前保存的会话会在打开平台后读取。";
  platforms.innerHTML = data.platforms.filter(s=>s.collection).map(s=>`<article class="platform"><strong>${esc(s.name)}</strong><span class="badge ${data.active&&s.chat_ready?'good':''}">${data.active&&s.chat_ready?'已找到聊天框':'等待检查'}</span><p style="font-size:13px;line-height:1.7;color:#66778e">${s.last_answer_at?`上次取得回答：${esc(s.last_answer_at)}`:'尚未在本机取得真实回答'}<br>${data.active?esc(s.message):'打开后检查当前登录状态'}</p><div class="buttons"><button class="primary" data-action="open" data-site="${esc(s.id)}" ${working?'disabled':''}>打开 ${esc(s.name.replace('（DP）',''))}</button><button data-action="check" data-site="${esc(s.id)}" ${working||!data.active?'disabled':''}>检查聊天框</button></div></article>`).join("");
}
async function run(payload) {
  if (busy) return;
  busy = true;
  document.querySelectorAll("button").forEach(b=>b.disabled=true);
  notice.className="";
  notice.textContent = payload.action === "open" ? "正在打开本机浏览器，请稍等…" : "正在检查…";
  try { render(await action(payload)); }
  catch(error) { if(latest)render(latest);notice.textContent=error.message;notice.className="error"; }
  finally { busy=false; document.getElementById("refresh").disabled=false; }
}
platforms.addEventListener("click",event=>{const button=event.target.closest("[data-action]");if(button)run({action:button.dataset.action,site:button.dataset.site});});
document.getElementById("refresh").onclick=()=>run({action:"status"});
document.getElementById("finish").onclick=()=>run({action:"finish"});
run({action:"status"});
