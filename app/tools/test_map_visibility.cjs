"use strict";
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
let clock=1000;
const sandbox={window:{},performance:{now:()=>clock+=.2}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../scrap_editor_app/web/map3d.js"),"utf8"),sandbox);
const prototype=sandbox.window.ScrapMap3D.prototype;
function renderer(){
  const r=Object.create(prototype);
  Object.assign(r,{worldMode:true,generation:1,canvas:{clientWidth:1000,clientHeight:800},camera:[0,0,1000],pitch:-Math.PI/2,yaw:0,distance:1000,worldIndex:{width:1000,height:1000},lastVisibility:0,visibilityInterval:0,visibilityDirty:true,visibilityApplyQueue:[],visibilityApplyCursor:0,spatialBuckets:new Map(),dynamicItems:[],results:new Map()});
  for(let g=0;g<100;g++){
    const centers=new Float32Array(40);for(let i=0;i<10;i++)centers.set([(g%10-5)*80+i,(Math.floor(g/10)-5)*80,0,10],i*4);
    const item={totalInstances:10,instanceCenters:centers,maxViewDistance:10000,definition:{lodCount:3,lodMinViewSizes:[80,20,0]}};
    r.dynamicItems.push(item);r.spatialBuckets.set(g,{groups:[{item,indices:Array.from({length:10},(_,i)=>i),x:centers[0],y:centers[1],z:0,radius:40}]});
  }
  r._applyVisibleIndices=function(item,indices){this.results.set(this.dynamicItems.indexOf(item),Array.from(indices))};
  r._finishVisibility=function(summary){this.summary=summary};
  return r;
}
const immediate=renderer();immediate._refreshVisibility(true);
const sliced=renderer();let frames=0;
do{sliced._refreshVisibility();frames++;assert.ok(frames<1000)}while(sliced.visibilityScan);
assert.ok(frames>1,"scan should yield across frames");
assert.equal(sliced.results.size,0,"keep the old visibility set until the complete scan is available");
while(sliced.visibilityApplyCursor<sliced.visibilityApplyQueue.length)sliced._flushVisibilityUpdates(4);
assert.deepEqual([...sliced.results],[...immediate.results]);
assert.equal(sliced.summary.visible,immediate.summary.visible);
const cancelled=renderer();cancelled._refreshVisibility();assert.ok(cancelled.visibilityScan);
cancelled.generation++;cancelled.dynamicItems=[];cancelled._refreshVisibility();assert.equal(cancelled.visibilityScan,null);
const capture=renderer();capture._refreshVisibility();capture.captureViewport={};capture._refreshVisibility();assert.equal(capture.visibilityScan,null);assert.equal(capture.results.size,0);
let layoutReads=0;
const lod=renderer();lod.canvas={get clientHeight(){layoutReads++;return 800}};lod.lodScreenHeight=800;
for(const item of lod.dynamicItems)for(let i=0;i<item.totalInstances;i++){
  const c=item.instanceCenters,o=i*4,d=Math.max(1,Math.hypot(c[o],c[o+1],c[o+2]-1000));
  const pixels=c[o+3]*2*800/(d*Math.tan(24*Math.PI/180));
  const expected=pixels>=80*.72?0:pixels>=20*.72?1:2;
  assert.equal(lod._desiredLod(item,i),expected);
}
assert.equal(layoutReads,0,"LOD evaluation must not read layout for every object");
console.log(`Visibility matches across ${frames} slices; cancellation, capture isolation, and 1000 LOD choices pass.`);
