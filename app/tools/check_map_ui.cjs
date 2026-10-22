"use strict";
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const root = path.resolve(__dirname, "..");
const output = process.env.MAP_TEST_OUTPUT
  ? path.resolve(process.env.MAP_TEST_OUTPUT)
  : path.join(root, "artifacts", "map-ui");
fs.mkdirSync(output, { recursive: true });
const web = path.join(root, "scrap_editor_app/web");
const image = pathToFileURL(path.join(web, "game-ui/map-preview.png")).href;
const features = [
  { key: "station", label: "Mechanic station", category: "stations", worldX: -810, worldY: -620, x: -810/64, y: -620/64 },
  { key: "warehouse", label: "Warehouse", category: "warehouses", worldX: -590, worldY: -480, x: -590/64, y: -480/64 },
];
const state = {
  contextRevision: 1,
  config: { ready: true, sourceSave: "test", sourceSeed: 1, mapWidth: 1024, mapHeight: 768, worldMinX: -4096, worldMaxX: 4096, worldMinY: -3072, worldMaxY: 3072, cellBounds: [-64,63,-48,47], mapQuality: "preview" },
  image, minimapImage: image,
  settings: { size: 280, worldSpan: 650, shape: "square", border: "mechanic", orientation: "map-up", updateHz: 60, fogOfWar: false, showCompass: true, showKeyHint: true },
  overlay: { mapReady: true, inGameWorld: true, directRenderer: true, livePush: false, busy: false },
  live: { available: true, updated: Date.now()/1000, state: { source: "game-memory", position: { x: -800,y: -800,z: 10 }, direction: { x: 0,y: 1,z: 0 }, waypoints: features, discovered: features.map(f=>f.key) } },
  exploration: { enabled: false, cells: [], points: [], samples: [], discovered: features.map(f=>f.key), radius: 192, revision: 1 },
};
let activeBrowser;
async function send(page, data=state) {
  await page.evaluate(value=>window.postMessage({type:"companion-preview-state",state:value},"*"),data);
}
(async()=>{
  const browser=await chromium.launch({headless:true,channel:process.env.MAP_TEST_BROWSER || "chrome",args:["--allow-file-access-from-files"]});
  activeBrowser=browser;
  const page=await browser.newPage({viewport:{width:280,height:280}});
  const errors=[];page.on("pageerror",error=>errors.push(error.message));
  await page.goto(pathToFileURL(path.join(web,"companion_minimap.html")).href+"?preview=1");
  await send(page);
  await page.waitForFunction(()=>document.querySelector("#buildStatus").classList.contains("quiet"));
  assert.equal(await page.locator("#buildStatus").evaluate(n=>n.classList.contains("quiet")),true);
  for(const shape of ["square","circle"]){
    await send(page,{...state,settings:{...state.settings,shape}});
    await page.waitForTimeout(80);
    assert.equal(await page.locator("main").evaluate((n,s)=>n.classList.contains(s),shape),true);
    await page.screenshot({path:path.join(output,`minimap-${shape}.png`)});
  }
  await send(page,{...state,settings:{...state.settings,showKeyHint:false}});
  assert.equal(await page.locator("#mapKey").evaluate(n=>n.hidden),true);
  const recovery=await browser.newPage({viewport:{width:280,height:280}});
  let imageAttempts=0;
  await recovery.route("https://map-test.invalid/**",route=>{imageAttempts++;return imageAttempts<3?route.abort():route.fulfill({contentType:"image/png",headers:{"access-control-allow-origin":"*"},body:fs.readFileSync(path.join(web,"game-ui/map-preview.png"))})});
  await recovery.goto(pathToFileURL(path.join(web,"companion_minimap.html")).href+"?preview=1");
  await send(recovery,{...state,image:"https://map-test.invalid/map.png",minimapImage:"https://map-test.invalid/map.png"});
  await recovery.waitForFunction(()=>document.querySelector("#buildStatus").classList.contains("quiet"));
  assert.equal(imageAttempts,3);

  const full=await browser.newPage({viewport:{width:1280,height:800}});
  full.on("pageerror",error=>errors.push(error.message));
  await full.goto(pathToFileURL(path.join(web,"companion_overlay.html")).href+"?preview=1");
  await send(full);
  await full.waitForSelector("#mapStage:not([hidden])");
  assert.equal(await full.locator(".marker.player").count(),1);
  await full.locator("#locationSearch").fill("Mechanic");
  assert.equal(await full.locator(".marker:not(.player)").count(),1);
  await full.locator("#locationSearch").fill("");
  await full.locator("#centerPlayer").click();
  await full.waitForTimeout(100);
  await full.locator(".marker.stations").click();
  assert.equal(await full.locator("#waypointDetailsName").textContent(),"Mechanic station");
  await full.screenshot({path:path.join(output,"full-map.png")});

  // Exercise the actual native-buffer output at a 144 Hz render clock.
  // Every moving output frame must contain its interpolated map transform.
  const native=await browser.newPage({viewport:{width:280,height:280}});
  native.on("pageerror",error=>errors.push(error.message));
  await native.addInitScript(()=>{
    window.requestAnimationFrame=callback=>setTimeout(()=>callback(performance.now()),7);
    window.cancelAnimationFrame=clearTimeout;
    window.testListeners={};window.testFrames=[];
    window.chrome.webview={addEventListener:(name,callback)=>window.testListeners[name]=callback};
  });
  await native.goto(pathToFileURL(path.join(web,"companion_minimap.html")).href+"?preview=1");
  await native.evaluate(()=>{
    const canvas=document.createElement("canvas");canvas.width=canvas.height=800;
    const ctx=canvas.getContext("2d");
    for(let x=0;x<800;x++){ctx.fillStyle=`rgb(${x%256},${(x*7)%256},${(x*13)%256})`;ctx.fillRect(x,0,1,800)}
    window.testImage=canvas.toDataURL();
    window.testBuffer=new ArrayBuffer(4+280*280*4);
    window.testListeners.sharedbufferreceived({additionalData:{type:"minimap-frame-buffer",width:280,height:280},getBuffer:()=>window.testBuffer});
    const original=window.pywebview.api.request;
    window.pywebview.api.request=async(method,payload)=>{
      if(method==="minimap-frame-signal"){
        const bytes=new Uint8Array(window.testBuffer);
        if(window.testRecording){let hash=0;for(let x=30;x<250;x+=11){const i=4+(70*280+x)*4;hash=((hash*31)+bytes[i]*3+bytes[i+1]*7+bytes[i+2]*13)|0}window.testFrames.push(hash)}
        new Uint32Array(window.testBuffer)[0]=0;
        return{ok:true};
      }
      return original(method,payload);
    };
  });
  const nativeState=structuredClone(state);nativeState.overlay.directRenderer=false;
  nativeState.config={...nativeState.config,worldMinX:-400,worldMaxX:400,worldMinY:-400,worldMaxY:400,mapWidth:800,mapHeight:800};
  nativeState.live.state.position={x:0,y:0,z:10};nativeState.live.state.waypoints=[];
  nativeState.image=nativeState.minimapImage=await native.evaluate(()=>window.testImage);
  await send(native,nativeState);
  await native.waitForTimeout(400);
  await native.evaluate(live=>{
    window.testRecording=true;let step=0;
    window.testMotion=setInterval(()=>{step++;live.updated=Date.now()/1000;live.state.position.x=step*2;window.companionMinimapLive(live)},16);
  },nativeState.live);
  await native.waitForTimeout(1400);
  const movement=await native.evaluate(()=>{clearInterval(window.testMotion);window.testRecording=false;return{frames:window.testFrames.length,unique:new Set(window.testFrames).size,error:window.__minimapLastFrameError}});
  assert.ok(movement.frames>35,JSON.stringify(movement));
  assert.ok(movement.unique/movement.frames>.88,JSON.stringify(movement));
  assert.equal(movement.error,undefined);
  await native.evaluate(()=>{
    const original=CanvasRenderingContext2D.prototype.drawImage;
    CanvasRenderingContext2D.prototype.drawImage=function(source,...args){
      if(source.id==="minimap"&&!this.canvas.isConnected){this.setTransform(1,0,0,1,0,0);return original.call(this,source,0,0,this.canvas.width,this.canvas.height)}
      return original.call(this,source,...args);
    };
  });
  nativeState.live.updated=Date.now()/1000;await send(native,nativeState);await native.waitForTimeout(250);
  await native.evaluate(live=>{window.testFrames=[];window.testRecording=true;let step=0;window.testMotion=setInterval(()=>{step++;live.updated=Date.now()/1000;live.state.position.x=step*2;window.companionMinimapLive(live)},16)},nativeState.live);
  await native.waitForTimeout(1400);
  const control=await native.evaluate(()=>{clearInterval(window.testMotion);window.testRecording=false;return{frames:window.testFrames.length,unique:new Set(window.testFrames).size}});
  assert.ok(control.unique/control.frames < movement.unique/movement.frames-.1,JSON.stringify({movement,control}));
  const lifecycle=await browser.newPage();
  lifecycle.on("pageerror",error=>errors.push(error.message));
  await lifecycle.goto(pathToFileURL(path.join(web,"companion_overlay.html")).href+"?preview=1");
  await lifecycle.addScriptTag({path:path.join(web,"map3d.js")});
  const teardown=await lifecycle.evaluate(async()=>{
    const canvas=document.createElement("canvas");canvas.width=canvas.height=128;document.body.append(canvas);
    const first=new window.ScrapMap3D(canvas,async()=>({}),()=>{});
    first.geometryPromises.set("retained",Promise.resolve(new Float32Array(1000)));
    first.dispose();first.dispose();
    const second=new window.ScrapMap3D(canvas,async()=>({}),()=>{});
    canvas.dispatchEvent(new KeyboardEvent("keydown",{key:"w",bubbles:true}));
    const result={oldKeys:first.keys.size,newKeys:second.keys.size,oldGeometry:first.geometryPromises.size,oldProgram:second.gl.isProgram(first.program)};
    second.dispose();
    try{await second.loadWorld();result.cancelled=false}catch(error){result.cancelled=/cancelled/.test(error.message)}
    canvas.remove();return result;
  });
  assert.deepEqual(teardown,{oldKeys:0,newKeys:1,oldGeometry:0,oldProgram:false,cancelled:true});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(output,"browser-results.json"),JSON.stringify({errors,imageAttempts,movement,control,teardown},null,2));
  console.log(JSON.stringify({errors,imageAttempts,movement,control,teardown}));
  await browser.close();
})().catch(async error=>{console.error(error);await activeBrowser?.close();process.exitCode=1});
