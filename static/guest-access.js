const form=document.getElementById('start-form');
const message=document.getElementById('access-message');
async function enter(path,payload){
 const button=document.getElementById('start-button');button.disabled=true;
 message.textContent='正在打开你的登录页面…';
 try{
  const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),cache:'no-store'});
  const result=await response.json();
  if(!response.ok)throw Error(result.error||'暂时无法进入，请重试');
  location.replace(result.next||'/login-center');
 }catch(error){message.textContent=error.message;button.disabled=false;}
}
form.addEventListener('submit',event=>{event.preventDefault();enter('/api/access/start',{name:document.getElementById('participant-name').value.trim()});});
const invite=new URLSearchParams(location.hash.slice(1)).get('invite');
if(invite){history.replaceState(null,'','/');enter('/api/access',{invite});}
