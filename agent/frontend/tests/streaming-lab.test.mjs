import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import ts from 'typescript'
import { compileScript, parse } from '@vue/compiler-sfc'
import * as vue from 'vue'

const { outputText } = ts.transpileModule(readFileSync(new URL('../src/utils/streamingLab.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
})
const api = {}
new Function('exports', outputText)(api)
const row = (tenant, window, revision, clicks, users) => ({ tenant_id: tenant, window_key: window, revision,
  state: 'completed', report: {}, invalidated: false, snapshot: { metrics: [{ news_id: 'news', clicks, unique_users: users }] } })

test('versions and differences never compare other tenants or windows', () => {
  const a = row('a','a/first',1,10,5), b = row('a','a/first',2,12,6)
  const foreign = row('b','b/first',99,100,99), later = row('a','a/second',99,200,199)
  assert.deepEqual(api.windowRevisions([a,foreign,b,later],'a','a/first'), [b,a])
  assert.equal(api.previousRevision([foreign,later,a,b],b), a)
  assert.equal(api.metricDelta(b,a,'news','clicks'),2)
  assert.equal(api.metricDelta(b,a,'news','unique_users'),1)
  assert.equal(api.metricDelta(b,foreign,'news','clicks'),null)
  assert.equal(api.metricDelta(b,later,'news','clicks'),null)
})

test('pending, missing reports and invalidated windows never display completed success', () => {
  const r = row('a','a/first',1,10,5)
  assert.equal(api.consumptionLabel(r),'分析完成')
  assert.equal(api.consumptionLabel({ ...r, state: 'started' }),'分析中')
  assert.equal(api.consumptionLabel({ ...r, report: null }),'报告缺失')
  assert.equal(api.consumptionLabel({ ...r, invalidated: true }),'窗口已失效')
})

function panel(t, run, operations = async () => ({}), runs = async () => ({runs:[]})) {
  const source=compileScript(parse(readFileSync(new URL('../src/views/StreamingLabView.vue',import.meta.url),'utf8')).descriptor,{id:'stream-lab-test'}).content
  const compiled=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText
  const lifecycle=[]
  const imports={
    vue:{...vue,onMounted(){},onUnmounted(fn){lifecycle.push(fn)}},
    'vue-router':{useRoute:()=>({query:{run:'test'}})},
    '@/api/streamingLab':{LAB_TENANTS:['a','b'],METRIC_LABELS:{clicks:'点击'},streamingLab:{run,operations,runs}},
    '@/utils/streamingLab':api,'@/utils/format':{formatDecimal:String},
  }
  const exports={}
  new Function('require','exports',compiled)((name)=>{assert.ok(Object.hasOwn(imports,name));return imports[name]},exports)
  const scope=vue.effectScope()
  const state=scope.run(()=>exports.default.setup({}, {expose(){}}))
  const unmount=()=>{lifecycle.forEach(fn=>fn());scope.stop()}
  t.after(unmount)
  return {state,unmount}
}
function deferred() { let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve} }

test('tenant switch aborts old reads and ignores late results even if transport ignores cancellation',async(t)=>{
  const requests=[]
  const {state}=panel(t,(_id,tenant,signal)=>{const pending=deferred();requests.push({tenant,signal,...pending});return pending.promise})
  const old=state.refresh()
  state.tenant.value='b'
  await vue.nextTick()
  assert.equal(requests.length,2)
  assert.equal(requests[0].signal.aborted,true)
  requests[1].resolve({run_id:'test',snapshots:[row('b','b/window',1,3,2)],rejections:[]})
  await vue.nextTick();await vue.nextTick()
  requests[0].resolve({run_id:'test',snapshots:[row('a','a/window',1,99,99)],rejections:[]})
  await old
  assert.equal(state.data.value.snapshots[0].tenant_id,'b')
  assert.equal(state.selected.value.tenant_id,'b')
})

test('failed refresh clears stale success and unmount cancels in-flight reads',async(t)=>{
  const pending=deferred();let call=0;let signal
  const {state,unmount}=panel(t,async(_id,_tenant,s)=>{signal=s;call++;if(call===1)throw new Error('unavailable');return pending.promise})
  state.data.value={snapshots:[row('a','a/window',1,5,3)]}
  await state.refresh()
  assert.equal(state.data.value,null)
  assert.equal(state.error.value,'unavailable')
  const loading=state.refresh()
  unmount();assert.equal(signal.aborted,true)
  pending.resolve({snapshots:[],rejections:[]});await loading
  assert.equal(state.data.value,null)
})

test('operations outage removes stale healthy status and latest window is selected first',async(t)=>{
  const {state}=panel(t,async()=>({snapshots:[]}),async()=>{throw new Error('unavailable')})
  state.operations.value={dispatcher:{state:'ready'}}
  await state.refreshOperations()
  assert.equal(state.operations.value,null)
  assert.equal(state.operationsError.value,'unavailable')
  state.data.value={snapshots:[row('a','a/run/1800000000000',1,10,5),row('a','a/run/1800000060000',1,20,6)]}
  assert.deepEqual(state.windows.value,['a/run/1800000060000','a/run/1800000000000'])
})

test('live following advances run but manual history selection stays fixed',async(t)=>{
  let latest='live-00000001'
  const {state}=panel(t,async(id)=>({run_id:id,snapshots:[]}),
    async()=>({dynamic_feed:{recent_batches:[{run_id:latest}]}}))
  state.followLive.value=true
  await state.refreshRuns();await vue.nextTick()
  assert.equal(state.runId.value,'live-00000001')
  latest='live-00000002'
  await state.refreshRuns();await vue.nextTick()
  assert.equal(state.runId.value,'live-00000002')
  state.followLive.value=false
  state.runId.value='history'
  await state.refreshRuns()
  assert.equal(state.runId.value,'history')
})
