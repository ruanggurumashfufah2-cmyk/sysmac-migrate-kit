// Exercises the shipped page in a DOM-free sandbox; no real projects or clipboard.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('scripts/workbench/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/loadProjects\(\);\s*$/, '');
const memory = new Map();
const sandbox = {assert, console, localStorage:{getItem:k=>memory.get(k)||null,setItem:(k,v)=>memory.set(k,v)}};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);
vm.runInContext(`
function task(i,extra={}){return {id:String(i),generation:'revision1',kind:'point',wire:'X2_I1_'+String(i).padStart(2,'0'),
  target_wire:'X2_I1_'+String(i).padStart(2,'0'),function:'检测'+i,var:'',reason:'未找到同地址旧映射',signal_kind:'DI',
  options:['new','source','unused','ignore','defer'].map(value=>({value})),sections:['输入刷新'],sources:[],...extra}}
const tasks=Array.from({length:600},(_,i)=>task(i));
const filter={group:'missing',station:'X2',state:'todo',limit:200};
assert.equal(filterQuestions(tasks,filter,()=>false).length,600);
Q_TASKS=tasks;Q_FILTER=filter;renderQuestions=()=>{};
qSelectShown();assert.equal(Q_PICKED.size,200);
qSelectFiltered();assert.equal(Q_PICKED.size,600);
assert.equal(filterQuestions(tasks,{...filter,module:'X2/DI/模块2'},()=>false).length,0);
assert.equal(filterQuestions(tasks,{...filter,state:'saved'},t=>+t.id<15).length,15);
assert.equal(questionGroup(task(1,{reason:'同地址多处赋值'})),'duplicate');
assert.equal(questionGroup(task(1,{reason:'方向或设备不一致',var:'DI_上升'})),'direction');
assert.equal(questionGroup(task(1,{var:'DI_备用'})),'spare');
assert.equal(questionGroup({kind:'module'}),'module');
assert.equal(questionGroup(task(1,{reason:'用户暂缓处理，继续保留待判断',problem_group:'direction'})),'direction');
assert.equal(questionGroup(task(1,{reason:'用户暂缓处理',problem_reason:'旧程序同一地址有多处赋值'})),'duplicate');
const mixed=[task(700,{var:'DI_备用',page:1}),task(701,{problem_group:'direction',page:2}),task(702,{page:3}),task(703,{problem_group:'naming',page:4})];
const arranged=orderQuestions(mixed);
assert.equal(arranged.map(questionGroup).join(','),'missing,naming,spare,direction');
assert.equal(orderQuestions(mixed,'drawing')[0].id,'700');assert.equal(mixed[0].id,'700');
assert.equal(orderQuestions([task(1,{station:'X10'}),task(2,{station:'X2'})])[0].station,'X2');
Q_TASKS=[...tasks,...mixed];Q_FILTER={...filter,group:'',order:'group'};toast=()=>{};
qSelectGroup('missing');assert.equal(Q_PICKED.size,601);assert.equal(Q_PICKED.has('700'),false);
Q_FILTER.q='检测599';qSelectGroup('missing');assert.equal(Q_PICKED.size,1);assert.equal(Q_PICKED.has('599'),true);
Q_FILTER={state:'all',group:'',order:'group'};Q_TASKS=mixed;Q_PICKED.clear();
const markup=qCompactTable(arranged,new Map(mixed.map((t,i)=>[t.id,i])));
assert.equal((markup.match(/class="q-group-row"/g)||[]).length,4);
assert.ok(markup.includes('选择整组 图纸有功能，缺少旧映射 1 项'));
assert.ok(qReasonHtml(task(1,{problem_reason:'原始问题',reason:'用户暂缓处理'})).includes('当前处理：用户暂缓处理'));
Q_TASKS=tasks;Q_FILTER=filter;
assert.equal(commonBatchActions(tasks).includes('source'),false);
const unique=task(1,{sources:[{simple:true,kind:'DI',section:'旧输入',line:2}]});
assert.equal(commonBatchActions([unique]).includes('source'),true);
assert.equal(commonBatchActions([unique,{...unique,sources:[...unique.sources,...unique.sources]}]).includes('source'),false);
const drafted=batchItems([task(1)],'new',{template:'{类型}_{工位}_{模块}_{通道}_{功能}',workstation:'放卷',section:'输入刷新'});
assert.equal(drafted[0].draft.values.var,'DI_放卷_1_1_检测1');
assert.throws(()=>batchNewName(task(1),'{未知}',''),/未知字段/);
assert.throws(()=>batchNewName(task(1),'{工位}_{功能}',''),/工位名称/);
const chosen=batchItems([unique],'source',{});
assert.equal(chosen[0].draft.values.source.line,2);
P='虚拟项目';Q_CATALOG={rows:[{var:'DI_Exists'}],appliedNames:new Set(['di_applied'])};
Q_TASKS=tasks;Q_BATCH={items:batchItems([task(1),task(2)],'new',{section:'输入刷新'})};
Q_BATCH.items[0].draft.values.var='DI_Same';Q_BATCH.items[1].draft.values.var='di_same';
assert.equal(qBatchErrors().size,2);
Q_BATCH.items[0].draft.values.var='di_exists';Q_BATCH.items[1].draft.values.var='DI_Applied';
assert.equal(qBatchErrors().size,2);
qWriteLocal('3',{action:'new',generation:'stale',values:{var:'DI_New'}});
Q_BATCH.items=[{task:task(1),draft:{action:'new',values:{var:'DI_New',section:'输入刷新'},remark:''}}];
assert.equal(qBatchErrors().size,0);
qWriteLocal('3',{action:'new',generation:'revision1',values:{var:'DI_New'}});
assert.equal(qBatchErrors().size,1);
Q_BATCH={items:[...chosen,...chosen.map(x=>({...x,task:task(2)}))]};
assert.equal(qBatchErrors().size,2);
qWriteLocal('3',null);
`, sandbox);
(async()=>{
  await vm.runInContext(`(async()=>{
    // The same production submit function supports new batch servers and running older servers.
    const calls=[];let regenerations=0,renders=0,closed=0;ST={};
    document={querySelector:()=>({close:()=>closed++})};qUpdateBatchErrors=()=>{};renderCards=()=>{};
    renderQuestions=()=>{assert.equal(Q_BUSY,false);renders++};toast=message=>console.log(message);
    runAll=async()=>{regenerations++};
    let batchMode=true,failAfter=Infinity;
    api=async(path,opt)=>{if(path==='/api')return batchMode?{'POST /api/p/<项目>/feedback_batch {}':'batch'}:{'POST /feedback':'single'};
      if(calls.length===failAfter)throw new Error('模拟中断');calls.push({path,body:JSON.parse(opt.body)});return {count:300}};
    function prepare(){Q_TASKS=tasks.slice(0,300);Q_PICKED=new Set(Q_TASKS.map(t=>t.id));Q_BATCH={project:P,action:'new',items:batchItems(Q_TASKS,'new',{section:'输入刷新'})};Q_CATALOG={rows:[]}}
    prepare();await qExecuteBatch(true);
    assert.equal(calls.length,1);assert.equal(calls[0].body.items.length,300);
    assert.equal(regenerations,1);assert.equal(Q_BUSY,false);assert.equal(Q_PICKED.size,0);assert.equal(Q_BATCH,null);
    // Older-server compatibility still saves all rows then regenerates once.
    calls.length=0;batchMode=false;prepare();await qExecuteBatch(true);
    assert.equal(calls.length,300);assert.equal(regenerations,2);assert.equal(renders,2);assert.equal(closed,2);
    // Interrupted fallback keeps confirmed rows, leaves the rest unmarked and does not regenerate.
    calls.length=0;prepare();localStorage.setItem(qLocalKey(),'{}');
    failAfter=2;
    await qExecuteBatch(true);assert.equal(calls.length,2);assert.equal(Q_PICKED.size,298);
    assert.equal(Object.keys(qLocal()).length,2);assert.equal(regenerations,2);assert.equal(Q_BUSY,false);
    console.log('PASS: compact review, full-filter selection, batch naming/collisions, 300-row saves, partial failure');
  })()`,sandbox);
})().catch(e=>{console.error(e);process.exitCode=1});
