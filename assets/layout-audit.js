(() => {
 const issues=[], vw=innerWidth, vh=innerHeight;
 const visible=e=>{const b=e.getBoundingClientRect(), s=getComputedStyle(e);return b.width>0&&b.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const identify=e=>e.id?'#'+CSS.escape(e.id):e.tagName.toLowerCase()+(e.getAttribute('aria-label')?'[aria-label='+JSON.stringify(e.getAttribute('aria-label'))+']':'');
 const named=e=>(e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||e.getAttribute('alt')||'').trim();
 for(const e of Array.from(document.querySelectorAll('body *')).slice(0,10000)) {
  if(!visible(e))continue;
  const b=e.getBoundingClientRect(), s=getComputedStyle(e), selector=identify(e);
  if(b.right>vw+1&&s.position!=='fixed'&&s.overflowX!=='hidden')issues.push({rule:'horizontal-overflow',selector,right:Math.round(b.right)});
  if(e.matches('img')&&!e.hasAttribute('alt'))issues.push({rule:'image-alt',selector});
  if(e.matches('button,a[href],[role="button"]')) {
   if(!named(e)&&!e.querySelector('img[alt],svg[aria-label]')&&!e.getAttribute('aria-labelledby'))issues.push({rule:'accessible-name',selector});
   if(b.width<24||b.height<24)issues.push({rule:'target-size-24',selector,width:Math.round(b.width),height:Math.round(b.height)});
  }
  if(e.matches('input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea')&&!(e.labels&&e.labels.length)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby'))issues.push({rule:'input-label',selector});
 }
 return {url:location.href,title:document.title,viewport:{width:vw,height:vh,dpr:devicePixelRatio},issues:issues.slice(0,500),truncated:issues.length>500,scope:'DOM heuristics; not a WCAG conformance audit'};
})()
