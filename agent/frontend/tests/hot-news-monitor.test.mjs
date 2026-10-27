import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { setImmediate } from 'node:timers/promises'
import { compileScript, parse } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'

const source=readFileSync(new URL('../src/components/conversation/HotNewsMonitorPanel.vue',import.meta.url),'utf8')
const dto=(extra={})=>({configured:true,can_manage:true,tenant_id:'tenant',source_state:'ready',monitors:[],items:[],...extra})
const card=(extra={})=>({notice_id:'stable',monitor_id:'m',news_id:'n',title:'热度新闻',window_start_ms:Date.now()-300000,
  window_end_ms:Date.now()-1000,expires_at_ms:Date.now()+200000,hot_score:0.3,...extra})
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve}}
const settle=async()=>{await setImmediate();await vue.nextTick()}

function panel(t,overrides={}) {
  const lifecycle=[]
  const streams=[]
  const api={snapshot:async()=>dto(),start:async()=>dto(),switch:async()=>dto(),
    subscribe(onFeed,onError){const stream={onFeed,onError,closed:false};streams.push(stream);return ()=>{stream.closed=true}},
    ...overrides}
  const document={hidden:false,addEventListener(){},removeEventListener(){}}
  const module=ts.transpileModule(compileScript(parse(source).descriptor,{id:'monitor-test'}).content,
    {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
  const exports={}
  const imports={vue:{...vue,onMounted(){},onBeforeUnmount(fn){lifecycle.push(fn)}},'@/api/hotNewsMonitor':{hotNewsMonitorApi:api}}
  new Function('require','exports','document',module)((name)=>{assert.ok(name in imports);return imports[name]},exports,document)
  const scope=vue.effectScope()
  const state=scope.run(()=>exports.default.setup({}, {expose(){}}))
  const unmount=()=>{lifecycle.forEach(fn=>fn());scope.stop()}
  t.after(unmount)
  return {state,streams,document,unmount}
}

test('stream refresh replaces cards without adding chat messages, and expiry removes them',async t=>{
  const p=panel(t)
  await p.state.connect()
  p.streams[0].onFeed(dto({items:[card()]}))
  assert.equal(p.state.cards.value.length,1)
  p.streams[0].onFeed(dto({items:[card({hot_score:0.5})]}))
  assert.equal(p.state.cards.value.length,1)
  assert.equal(p.state.cards.value[0].hot_score,0.5)
  p.state.clock.value=Date.now()+300000
  assert.deepEqual(p.state.cards.value,[])
  assert.ok(!source.includes('v-html') && !source.includes('sendStream'))
})

test('disconnect clears success; late events from a closed subscription cannot revive it',async t=>{
  const p=panel(t)
  await p.state.connect()
  const old=p.streams[0]
  old.onFeed(dto({items:[card()]}))
  old.onError()
  assert.deepEqual(p.state.cards.value,[])
  await p.state.connect()
  assert.equal(old.closed,true)
  old.onFeed(dto({items:[card()]}))
  assert.deepEqual(p.state.cards.value,[])
  p.unmount()
  assert.equal(p.streams[1].closed,true)
})

test('disabled backend does not open an EventSource or claim to be monitoring',async t=>{
  const p=panel(t,{snapshot:async()=>dto({configured:false,source_state:'disabled'})})
  await p.state.connect()
  assert.equal(p.streams.length,0)
  assert.equal(p.state.active.value,false)
})

test('inflight fetch is aborted on unmount and ignores a late response',async t=>{
  const waiting=deferred();let signal
  const p=panel(t,{snapshot:s=>{signal=s;return waiting.promise}})
  const work=p.state.connect()
  p.unmount()
  assert.equal(signal.aborted,true)
  waiting.resolve(dto({items:[card()]}))
  await work
  assert.equal(p.state.feed.value,null)
})

test('admin starts selected window once; ordinary reader cannot mutate',async t=>{
  const calls=[]
  const p=panel(t,{start:async minutes=>{calls.push(minutes);return dto()}})
  await p.state.connect()
  p.state.minutes.value=15
  await p.state.start()
  assert.deepEqual(calls,[15])
  p.state.feed.value=dto({can_manage:false})
  await p.state.start()
  assert.deepEqual(calls,[15])
})

test('uncertain write is not retried; state is read back after failure',async t=>{
  let writes=0,reads=0
  const p=panel(t,{snapshot:async()=>{reads++;return dto()},
    start:async()=>{writes++;throw new Error('private')}})
  await p.state.connect()
  await p.state.start()
  assert.equal(writes,1)
  assert.equal(reads,2)
  assert.match(p.state.operationError.value,/待确认/)
  assert.ok(!p.state.operationError.value.includes('private'))
})

test('hidden tab closes subscription and visible tab reconnects without replay appending',async t=>{
  const p=panel(t)
  await p.state.connect()
  p.document.hidden=true
  p.state.visibilityChanged()
  assert.equal(p.streams[0].closed,true)
  p.document.hidden=false
  p.state.visibilityChanged()
  await settle()
  assert.equal(p.streams.length,2)
})


test('SSE renewal preserves cards, reconnects with cookies, and cleanup stops the renewed connection',()=>{
  const instances=[]
  class EventSource {
    constructor(url,options) { this.url=url;this.options=options;this.handlers={};this.closed=false;instances.push(this) }
    addEventListener(name,handler) { this.handlers[name]=handler }
    close() { this.closed=true }
  }
  const text=readFileSync(new URL('../src/api/hotNewsMonitor.ts',import.meta.url),'utf8')
  const compiled=ts.transpileModule(text,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
  const exports={}
  new Function('require','exports','EventSource',compiled)(()=>({buildUrl:x=>x,request(){throw new Error('unused')}}),exports,EventSource)
  const seen=[];let failures=0
  const close=exports.hotNewsMonitorApi.subscribe(value=>seen.push(value),()=>{failures++})
  const first=instances[0]
  assert.equal(first.url,'/api/v1/hot-news-monitor/events')
  assert.equal(first.options.withCredentials,true)
  first.handlers.hotspots({data:JSON.stringify(dto({items:[card()]}))})
  first.handlers.reconnect()
  assert.equal(first.closed,true)
  assert.equal(instances.length,2)
  assert.equal(failures,0)
  first.handlers.hotspots({data:JSON.stringify(dto())})
  assert.equal(seen.length,1)
  close()
  assert.equal(instances[1].closed,true)
  instances[1].handlers.reconnect()
  assert.equal(instances.length,2)
})

