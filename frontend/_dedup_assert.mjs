function dedupeMessages(messages){
  const realByKey = new Map();
  for (const m of messages) if (m.id>0) realByKey.set(`${m.role}|${m.content}`, m);
  const out=[];
  for (const m of messages){
    if (m.id>0){ if(!out.includes(m)) out.push(m); }
    else if(!realByKey.has(`${m.role}|${m.content}`)) out.push(m);
  }
  return out.sort((a,b)=>a.id-b.id);
}
const dirty=[{id:0,role:'assistant',content:'深圳天气我没法查哦'},{id:7,role:'assistant',content:'深圳天气我没法查哦'},{id:6,role:'user',content:'那深圳呢'},{id:0,role:'user',content:'今天天气'}];
const r=dedupeMessages(dirty);
console.log('去重后条数:', r.length, '(应=3)');
console.log('含 id:0? ', r.some(m=>m.id<=0), '(应=false)');
console.log(r.map(m=>`${m.id}:${m.role}`).join(' '));
console.log((r.length===3 && !r.some(m=>m.id<=0))? 'PASS':'FAIL');
