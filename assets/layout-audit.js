(() => {
 const issues=[], vw=innerWidth, vh=innerHeight;
 const layout=window.__tomaccoViewport||null, platform=(layout&&layout.platform)||'generic';
 const safe=(layout&&layout.safeArea)||{top:0,right:0,bottom:0,left:0};
 // Apple's 44 pt and Android's 48 dp minimum touch targets.
 const minimum=platform==='android'?48:44;
 const visible=e=>{const b=e.getBoundingClientRect(), s=getComputedStyle(e);return b.width>0&&b.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
 const unique=id=>{try{return document.querySelectorAll('#'+CSS.escape(id)).length===1}catch(e){return false}};
 // A CSS selector that matches this element alone: its id, or a path of nth-of-type steps from the nearest unique id or body.
 const selectorOf=e=>{
  const parts=[];
  for(let n=e;n&&n.nodeType===1&&n!==document.documentElement;n=n.parentElement){
   if(n.id&&unique(n.id)){parts.unshift('#'+CSS.escape(n.id));break}
   let part=n.tagName.toLowerCase();
   const parent=n.parentElement;
   if(parent){const same=Array.from(parent.children).filter(c=>c.tagName===n.tagName);if(same.length>1)part+=':nth-of-type('+(same.indexOf(n)+1)+')'}
   parts.unshift(part);
  }
  return parts.join(' > ');
 };
 const named=e=>(e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||e.getAttribute('alt')||'').trim();
 const label=e=>named(e).replace(/\s+/g,' ').slice(0,60);
 const rect=b=>({x:Math.round(b.left),y:Math.round(b.top),width:Math.round(b.width),height:Math.round(b.height)});
 const root=document.scrollingElement||document.documentElement;
 if(root.scrollWidth>vw+1&&getComputedStyle(root).overflowX!=='hidden'&&!(document.body&&getComputedStyle(document.body).overflowX==='hidden'))
  issues.push({rule:'document-overflow',selector:'html',scroll_width:root.scrollWidth,viewport_width:vw,overflow:root.scrollWidth-vw});
 const interactive='button,a[href],[role="button"],[role="link"],[role="checkbox"],[role="switch"],[role="tab"],[role="menuitem"],input:not([type=hidden]),select,textarea,summary';
 for(const e of Array.from(document.querySelectorAll('body *')).slice(0,10000)) {
  if(!visible(e))continue;
  const b=e.getBoundingClientRect(), s=getComputedStyle(e), selector=selectorOf(e);
  if((b.right>vw+1||b.left<-1)&&s.position!=='fixed'&&s.overflowX!=='hidden')issues.push({rule:'horizontal-overflow',selector,right:Math.round(b.right),left:Math.round(b.left),viewport_width:vw});
  if(e.matches('img')&&!e.hasAttribute('alt'))issues.push({rule:'image-alt',selector});
  if(e.matches('button,a[href],[role="button"]')) {
   if(!named(e)&&!e.querySelector('img[alt],svg[aria-label]')&&!e.getAttribute('aria-labelledby'))issues.push({rule:'accessible-name',selector});
   if(b.width<24||b.height<24)issues.push({rule:'target-size-24',selector,width:Math.round(b.width),height:Math.round(b.height)});
  }
  // Links inside running text are exempt, as in WCAG's target-size rule.
  if(e.matches(interactive)&&(b.width<minimum||b.height<minimum)&&!(e.matches('a[href]')&&s.display==='inline'&&e.closest('p,li,td,dd')))
   issues.push({rule:'target-size-platform',selector,label:label(e),platform,minimum,width:Math.round(b.width),height:Math.round(b.height)});
  if(e.matches('input:not([type=hidden]):not([type=submit]):not([type=button]),select,textarea')&&!(e.labels&&e.labels.length)&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby'))issues.push({rule:'input-label',selector});
 }
 // Text where the profile's safe area ends: under the camera cutout, the system bars or a rounded edge.
 if(safe.top||safe.right||safe.bottom||safe.left) {
  const seen=new Set(), walker=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT);
  let count=0;
  for(let node=walker.nextNode();node&&count<5000;node=walker.nextNode()) {
   const parent=node.parentElement;
   if(!node.nodeValue.trim()||!parent||seen.has(parent)||!visible(parent))continue;
   count++;
   const range=document.createRange();range.selectNodeContents(node);
   for(const r of Array.from(range.getClientRects())) {
    if(r.width<1||r.height<1||r.bottom<=0||r.top>=vh||r.right<=0||r.left>=vw)continue;
    const edges=[];
    if(safe.top&&r.top<safe.top)edges.push('top');
    if(safe.bottom&&r.bottom>vh-safe.bottom)edges.push('bottom');
    if(safe.left&&r.left<safe.left)edges.push('left');
    if(safe.right&&r.right>vw-safe.right)edges.push('right');
    if(edges.length){seen.add(parent);issues.push({rule:'safe-area-text',selector:selectorOf(parent),edges,text:node.nodeValue.trim().replace(/\s+/g,' ').slice(0,60),rect:rect(r),safe_area:safe});break}
   }
  }
 }
 return {url:location.href,title:document.title,viewport:{width:vw,height:vh,dpr:devicePixelRatio},platform,safe_area:safe,target_minimum:minimum,issues:issues.slice(0,500),truncated:issues.length>500,scope:'DOM heuristics; not a WCAG conformance audit'};
})()
