import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";

import {
  browserPath,
  installDnsGuards,
  runBrowserScenario,
  startLoopbackServer,
  withMatrixDeadline,
} from "./browser-harness.mjs";

const NONCE = "YWJjZGVmZ2hpamtsbW5vcA==";
const CANARY = "PRIVATE-CONTENT-CANARY";
const capabilities = JSON.parse(
  await readFile(
    new URL("../../backend/app/capabilities.json", import.meta.url),
    "utf8",
  ),
);

const bundle = await build({
  entryPoints: [new URL("../src/index.ts", import.meta.url).pathname],
  bundle: true,
  minify: true,
  write: false,
});
const widgetBytes = bundle.outputFiles[0].contents;
const servedWidgetBytes = await readFile(
  new URL("../../backend/app/static/widget/widget.js", import.meta.url),
);
assert.deepEqual(
  Buffer.from(widgetBytes),
  servedWidgetBytes,
  "the served widget distribution must match the in-memory production build byte-for-byte",
);
const widgetSource = new TextDecoder().decode(widgetBytes);
const browser = await browserPath();
const apiRequests = [];
const siteMarkers = [];
let currentScenario = "";
let siteOrigin = "";

function corsHeaders() {
  return currentScenario === "cors" ? {} : { "access-control-allow-origin": siteOrigin, vary: "Origin" };
}

function sse(name, payload) {
  return `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
}

async function requestBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const apiServer = await startLoopbackServer(async (request, response) => {
  apiRequests.push({
    scenario: currentScenario,
    method: request.method,
    url: request.url,
    origin: request.headers.origin ?? null,
    accept: request.headers.accept ?? null,
    contentType: request.headers["content-type"] ?? null,
    cookie: request.headers.cookie ?? null,
    authorization: request.headers.authorization ?? null,
    referer: request.headers.referer ?? null,
    body: request.method === "POST" ? await requestBody(request) : "",
  });
  if (request.url === "/widget/widget.js") {
    response.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
    response.end(widgetSource);
    return;
  }
  if (request.url === "/api/v1/capabilities") {
    const manifest = currentScenario === "mismatch"
      ? { ...capabilities, compatibility: { ...capabilities.compatibility, widget: "0.1.0" } }
      : capabilities;
    response.writeHead(200, { "content-type": "application/json", ...corsHeaders() });
    response.end(JSON.stringify(manifest));
    return;
  }
  if (request.url === "/api/v1/chat/message" && request.method === "OPTIONS") {
    response.writeHead(204, {
      ...corsHeaders(),
      "access-control-allow-methods": "POST",
      "access-control-allow-headers": "content-type",
    });
    response.end();
    return;
  }
  if (request.url === "/api/v1/chat/message" && request.method === "POST") {
    response.writeHead(200, { "content-type": "text/event-stream; charset=utf-8", ...corsHeaders() });
    if (currentScenario === "refusal") {
      response.end(
        sse("status", { type: "status", state: "refusing", label: "No confident match found" }) +
        sse("chunk", { type: "chunk", delta: "I do not have a supported answer." }) +
        sse("done", { type: "done", finish_reason: "refused" }),
      );
    } else if (currentScenario === "error") {
      response.end(
        sse("status", { type: "status", state: "generating", label: "Generating a reply" }) +
        sse("error", { type: "error", code: "provider_unavailable", message: "The service is unavailable.", retryable: false }),
      );
    } else {
      const scenarioPosts = apiRequests.filter((item) => item.scenario === currentScenario && item.method === "POST").length;
      const prefix = sse("status", { type: "status", state: "retrieving", label: "Searching the knowledge base" });
      const citations = currentScenario === "desktop" && scenarioPosts === 1
        ? sse("citations", { type: "citations", sources: [{ id: "doc-1", title: CANARY, url: `${siteOrigin}/privacy` }] })
        : "";
      const answer = currentScenario === "mobile" ? `Answer ${"LONGTOKEN".repeat(75)}` : scenarioPosts === 1 ? "First answer" : "Second answer";
      response.write(prefix + citations + sse("status", { type: "status", state: "generating", label: "Generating a reply" }) + sse("chunk", { type: "chunk", delta: answer }));
      setTimeout(() => response.end(sse("done", { type: "done", finish_reason: "stop" })), 80);
    }
    return;
  }
  response.writeHead(404, { "content-type": "text/plain", ...corsHeaders() });
  response.end("not found");
});

function page(scenario) {
  const strictCsp = scenario === "csp" || scenario === "csp-negative";
  const hostNonce = scenario === "csp-negative" ? "" : strictCsp ? ` nonce="${NONCE}"` : "";
  const theme = scenario === "error" ? "dark" : scenario === "refusal" ? "light" : "auto";
  const csp = strictCsp
    ? `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${NONCE}'; style-src 'nonce-${NONCE}'; connect-src ${apiServer.origin}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'">`
    : "";
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">${csp}<script src="${apiServer.origin}/widget/widget.js" nonce="${NONCE}"></script></head><body><cairn-chat api-url="${apiServer.origin}" assistant-name="${scenario === "mobile" ? "客服😀".repeat(20) : "Cairn"}" theme="${theme}" privacy-url="${siteOrigin}/privacy" handoff-url="${siteOrigin}/support"${hostNonce}></cairn-chat><pre id="result"></pre><script nonce="${NONCE}">(async()=>{
  const scenario=${JSON.stringify(scenario)},CANARY=${JSON.stringify(CANARY)},NONCE=${JSON.stringify(NONCE)},host=document.querySelector("cairn-chat"),root=host.shadowRoot,result=document.querySelector("#result"),events=[],violations=[];
  const stateMirrors=(needle)=>{const seen=new Set(),contains=(value)=>{if(typeof value==="string")return value.includes(needle);if(value===null||typeof value!=="object"||seen.has(value))return false;const prototype=Object.getPrototypeOf(value);if(!Array.isArray(value)&&prototype!==Object.prototype&&prototype!==null)return false;seen.add(value);return Object.values(value).some(contains)};return Object.entries(host).filter(([key,value])=>key!=="styleElement"&&contains(value)).map(([key])=>key)};
  for(const name of ["cairn-open","cairn-close","cairn-complete","cairn-error","cairn-handoff","cairn-clear"])host.addEventListener(name,(event)=>events.push({name,detail:event.detail,bubbles:event.bubbles,composed:event.composed,cancelable:event.cancelable}));
  addEventListener("securitypolicyviolation",(event)=>violations.push({directive:event.violatedDirective,blocked:event.blockedURI}));
  const waitFor=async(predicate)=>{const started=performance.now();while(!predicate()){if(performance.now()-started>10000)throw new Error("fixture state timeout");await new Promise(resolve=>setTimeout(resolve,20));}};
  const rectangle=(element)=>{const b=element.getBoundingClientRect();return{top:b.top,right:b.right,bottom:b.bottom,left:b.left,width:b.width,height:b.height}};
  const contained=(inner,outer)=>inner.top>=outer.top-.5&&inner.left>=outer.left-.5&&inner.right<=outer.right+.5&&inner.bottom<=outer.bottom+.5;
  const color=(value)=>value.match(/[0-9.]+/g).slice(0,3).map(Number),luminance=(value)=>{const channels=color(value).map(component=>{const linear=component/255;return linear<=.04045?linear/12.92:((linear+.055)/1.055)**2.4});return .2126*channels[0]+.7152*channels[1]+.0722*channels[2]},contrast=(foreground,background)=>{const a=luminance(foreground),b=luminance(background);return(Math.max(a,b)+.05)/(Math.min(a,b)+.05)},contrastPair=(foreground,background)=>contrast(getComputedStyle(foreground).color,getComputedStyle(background).backgroundColor);
  const contrasts=()=>({panel:contrastPair(root.querySelector(".title"),root.querySelector(".panel")),empty:contrastPair(root.querySelector(".empty"),root.querySelector(".panel")),label:contrastPair(root.querySelector("label"),root.querySelector(".panel")),disclosure:contrastPair(root.querySelector(".disclosure"),root.querySelector(".panel")),input:contrastPair(root.querySelector("textarea"),root.querySelector("textarea")),launcher:contrastPair(root.querySelector(".launcher"),root.querySelector(".launcher")),send:contrastPair(root.querySelector(".send"),root.querySelector(".send"))});
  const geometry=()=>{const panelElement=root.querySelector(".panel"),panel=rectangle(panelElement),messagesElement=root.querySelector(".messages"),messages=rectangle(messagesElement),composer=rectangle(root.querySelector("form")),footer=rectangle(root.querySelector("footer")),viewport={top:0,left:0,right:innerWidth,bottom:innerHeight},isVisible=(element)=>{const style=getComputedStyle(element);return style.display!=="none"&&style.visibility!=="hidden"&&style.opacity!=="0"&&element.getClientRects().length>0},visible=[...root.querySelectorAll("button,a")].filter(isVisible),containedElements=[...root.querySelectorAll("button,a,.error,.disclosure")].filter(isVisible).map(element=>({element,bounds:rectangle(element)}));return{panel,messages,composer,footer,targets:visible.map(rectangle),documentScrollWidth:document.documentElement.scrollWidth,panelScrollWidth:panelElement.scrollWidth,panelClientWidth:panelElement.clientWidth,panelInside:contained(panel,viewport),contentInside:containedElements.every(({element,bounds})=>contained(bounds,viewport)&&(element.classList.contains("launcher")||contained(bounds,panel))),noOverlap:messages.bottom<=composer.top+.5&&composer.bottom<=footer.bottom+.5,noHorizontalOverflow:document.documentElement.scrollWidth<=innerWidth&&panelElement.scrollWidth<=panelElement.clientWidth,targets44:visible.every(element=>{const b=element.getBoundingClientRect();return b.width>=44&&b.height>=44}),newestVisible:messagesElement.scrollTop+messagesElement.clientHeight>=messagesElement.scrollHeight-.5}};
  const submit=async(text)=>{const input=root.querySelector("textarea");input.value=text;input.dispatchEvent(new Event("input",{bubbles:true}));root.querySelector("form").requestSubmit();await waitFor(()=>root.querySelector(".messages").getAttribute("aria-busy")==="true");const streaming={busy:root.querySelector(".messages").getAttribute("aria-busy"),disabled:input.disabled};await waitFor(()=>events.some(event=>event.name==="cairn-complete"||event.name==="cairn-error"));return streaming};
  try{
    if(scenario!=="csp"&&scenario!=="csp-negative")await fetch("/mark",{cache:"no-store"});
    const ids=[];const second=document.createElement("cairn-chat");second.setAttribute("api-url","${apiServer.origin}");if(host.nonce)second.nonce=host.nonce;document.body.append(second);ids.push(root.querySelector(".launcher").getAttribute("aria-controls"),second.shadowRoot.querySelector(".launcher").getAttribute("aria-controls"));second.remove();
    const closedLauncher=rectangle(root.querySelector(".launcher"));
    root.querySelector(".launcher").click();
    const checking={inputDisabled:root.querySelector("textarea").disabled,text:root.querySelector(".empty").textContent,focused:root.activeElement?.className};
    if(scenario==="mismatch"||scenario==="cors"){
      await waitFor(()=>events.some(event=>event.name==="cairn-error"));
      result.textContent=JSON.stringify({checking,events,violations,sendDisabled:root.querySelector(".send").disabled,error:root.querySelector(".error").textContent,idsUnique:ids[0]!==ids[1]});
    }else{
      await waitFor(()=>!root.querySelector("textarea").disabled);
        const ready={focused:root.activeElement===root.querySelector("textarea"),disclosure:root.querySelector(".disclosure").textContent,privacy:{href:root.querySelector(".privacy").href,target:root.querySelector(".privacy").target,rel:root.querySelector(".privacy").rel,referrerPolicy:root.querySelector(".privacy").referrerPolicy},contrasts:contrasts()};
      if(scenario==="mobile"){
        const input=root.querySelector("textarea");input.value="😀".repeat(500);input.dispatchEvent(new Event("input",{bubbles:true}));const exactCounter=root.querySelector(".counter").textContent;input.value="😀".repeat(501);input.dispatchEvent(new Event("input",{bubbles:true}));const overInvalid=input.getAttribute("aria-invalid");const streaming=await submit(CANARY);await waitFor(()=>events.some(event=>event.name==="cairn-complete"));result.textContent=JSON.stringify({checking,ready,streaming,exactCounter,overInvalid,geometry:geometry(),closedLauncher,events,idsUnique:ids[0]!==ids[1],longWrapped:root.querySelector(".assistant .message").scrollWidth<=root.querySelector(".assistant .message").clientWidth});
      }else if(scenario==="refusal"||scenario==="error"){
        await submit(CANARY);await waitFor(()=>!root.querySelector(".handoff").hidden);let canceled=false;host.addEventListener("cairn-handoff",(event)=>{event.preventDefault();canceled=true},{once:true});root.querySelector(".handoff").click();result.textContent=JSON.stringify({checking,ready,events,violations,handoff:{visible:!root.querySelector(".handoff").hidden,href:root.querySelector(".handoff").href,target:root.querySelector(".handoff").target,rel:root.querySelector(".handoff").rel,referrerPolicy:root.querySelector(".handoff").referrerPolicy,canceled},errorVisible:!root.querySelector(".error").hidden,geometry:geometry()});
      }else if(scenario==="desktop"){
        await submit(CANARY);const firstEventCount=events.length;events.splice(0,events.length);await submit("follow-up");const citation=root.querySelector(".citation");const changedNonce="Q0hBTkdFRC1OT05DRQ==";host.nonce=changedNonce;await Promise.resolve();const changedStyle=root.querySelector("style"),nonceChange={style:changedStyle.nonce,configuration:JSON.stringify(host.configuration),mirrors:stateMirrors(changedNonce)};host.nonce="";await Promise.resolve();const clearedNonce={style:root.querySelector("style").nonce,configuration:JSON.stringify(host.configuration),mirrors:stateMirrors(changedNonce)};const sessionBefore=sessionStorage.getItem("cairn-chat-session-id");root.querySelector(".clear").click();const sessionAfter=sessionStorage.getItem("cairn-chat-session-id");result.textContent=JSON.stringify({checking,ready,events,firstEventCount,citation:{text:citation?.textContent,href:citation?.href,target:citation?.target,rel:citation?.rel,referrerPolicy:citation?.referrerPolicy},nonceChange,clearedNonce,cleared:root.querySelectorAll("article").length===0,sessionRotated:sessionBefore!==sessionAfter,canaryRetained:root.textContent.includes(CANARY),storageKeys:Object.keys(localStorage),geometry:geometry()});
      }else if(scenario==="keyboard"){
        const input=root.querySelector("textarea"),launcher=root.querySelector(".launcher");input.focus();input.value=CANARY;input.dispatchEvent(new Event("input",{bubbles:true}));input.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}));await waitFor(()=>events.some(event=>event.name==="cairn-complete"));input.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,composed:true}));await Promise.resolve();result.textContent=JSON.stringify({checking,ready,events,closed:root.querySelector(".panel").hidden,expanded:launcher.getAttribute("aria-expanded"),launcherVisibility:getComputedStyle(launcher).visibility,launcherDisplay:getComputedStyle(launcher).display,launcherFocused:root.activeElement===launcher,rootActive:root.activeElement?.className,documentActive:document.activeElement?.localName,reduced:getComputedStyle(launcher).transitionDuration,forced:getComputedStyle(root.querySelector(".send")).borderStyle,geometry:geometry()});
      }else if(scenario==="safe"){
        result.textContent=JSON.stringify({checking,ready,events,geometry:geometry(),closedLauncher});
      }else{
        await new Promise(resolve=>setTimeout(resolve,50));const style=root.querySelector("style"),configurationBefore=JSON.stringify(host.configuration),mirrorsBefore=stateMirrors(NONCE);host.setAttribute("privacy-url","javascript:invalid");await Promise.resolve();const invalidConfiguration=host.configuration===null;host.setAttribute("privacy-url",${JSON.stringify(siteOrigin + "/privacy")});await waitFor(()=>host.configuration!==null&&!root.querySelector("textarea").disabled);const recoveredConfiguration=JSON.stringify(host.configuration),recoveredStyleNonce=root.querySelector("style").nonce;root.querySelector(".clear").click();const afterClearConfiguration=JSON.stringify(host.configuration),mirrorsAfter=stateMirrors(NONCE);result.textContent=JSON.stringify({checking,ready,events,violations,styleNonce:style.nonce,styleAttribute:style.getAttribute("nonce"),hostNonce:host.nonce,hostAttribute:host.getAttribute("nonce"),configurationBefore,mirrorsBefore,invalidConfiguration,recoveredConfiguration,recoveredStyleNonce,afterClearConfiguration,mirrorsAfter,styled:getComputedStyle(root.querySelector(".launcher")).position==="static"&&getComputedStyle(root.querySelector(".launcher")).minHeight==="52px",geometry:geometry()});
      }
    }
  }catch(error){result.textContent=JSON.stringify({fixtureError:String(error),events,violations});}
  })();</script></body></html>`;
}

const siteServer = await startLoopbackServer((request, response) => {
  if (request.url === "/mark") {
    siteMarkers.push(currentScenario);
    response.writeHead(204);
    response.end();
    return;
  }
  if (request.url?.startsWith("/fixture")) {
    response.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "set-cookie": `cairn_browser_canary=${CANARY}; SameSite=Lax`,
    });
    response.end(page(currentScenario));
    return;
  }
  response.writeHead(200, { "content-type": "text/plain" });
  response.end("operator-owned fixture");
});
siteOrigin = siteServer.origin;

async function scenario(name, viewport, options = {}) {
  currentScenario = name;
  const before = apiRequests.length;
  let output;
  try {
    output = await runBrowserScenario({
      browser,
      url: `${siteOrigin}/fixture?scenario=${name}`,
      viewport,
      allowedOrigins: [siteOrigin, apiServer.origin],
      ...options,
    });
  } catch (error) {
    throw new Error(`${name} failed after requests ${JSON.stringify(apiRequests.slice(before))}`, { cause: error });
  }
  assert.equal(output.value.fixtureError, undefined, `${name}: ${output.value.fixtureError ?? "fixture passed"}`);
  return { ...output, api: apiRequests.slice(before) };
}

const detailKeys = {
  "cairn-open": ["version"],
  "cairn-close": ["reason", "version"],
  "cairn-complete": ["citationCount", "finishReason", "version"],
  "cairn-error": ["kind", "retryable", "version"],
  "cairn-handoff": ["reason", "version"],
  "cairn-clear": ["version"],
};

function assertHostEvents(events) {
  for (const event of events) {
    assert.equal(event.bubbles, true);
    assert.equal(event.composed, true);
    assert.equal(event.cancelable, event.name === "cairn-handoff");
    assert.deepEqual(Object.keys(event.detail).sort(), detailKeys[event.name]);
  }
  assert.equal(JSON.stringify(events).includes(CANARY), false);
}

function assertGeometry(result) {
  assert.equal(result.panelInside, true);
  assert.equal(result.contentInside, true);
  assert.equal(result.noOverlap, true);
  assert.equal(result.noHorizontalOverflow, true);
  assert.equal(result.targets44, true);
}

function assertNormalTextContrast(contrasts) {
  for (const [name, ratio] of Object.entries(contrasts)) {
    assert.equal(ratio >= 4.5, true, `${name} contrast ${ratio} must be at least 4.5:1`);
  }
}

const dnsGuard = installDnsGuards();
try {
  await dnsGuard.verify();
  await withMatrixDeadline(async () => {
    const mobile = await scenario("mobile", { width: 320, height: 568 });
    assert.deepEqual(mobile.value.checking, { inputDisabled: true, text: "Checking service compatibility…", focused: "close" });
    assert.equal(mobile.value.ready.focused, true);
    assert.equal(mobile.value.streaming.busy, "true");
    assert.equal(mobile.value.streaming.disabled, true);
    assert.equal(mobile.value.exactCounter, "500 / 500");
    assert.equal(mobile.value.overInvalid, "true");
    assertGeometry(mobile.value.geometry);
    assert.equal(mobile.value.geometry.newestVisible, true);
    assertNormalTextContrast(mobile.value.ready.contrasts);
    assert.equal(mobile.value.longWrapped, true);
    assert.equal(mobile.value.idsUnique, true);
    assert.equal(mobile.value.closedLauncher.width >= 44 && mobile.value.closedLauncher.height >= 44, true);
    assertHostEvents(mobile.value.events);

    const refusal = await scenario("refusal", { width: 390, height: 844 });
    assert.equal(refusal.value.handoff.visible, true);
    assert.equal(refusal.value.handoff.canceled, true);
    assert.equal(refusal.value.errorVisible, false);
    assert.equal(refusal.value.events.at(-2).detail.finishReason, "refused");
    assertGeometry(refusal.value.geometry);
    assert.equal(refusal.value.geometry.newestVisible, true);
    assertNormalTextContrast(refusal.value.ready.contrasts);
    assertHostEvents(refusal.value.events);
    const terminalError = await scenario("error", { width: 390, height: 844 });
    assert.equal(terminalError.value.handoff.visible, true);
    assert.equal(terminalError.value.errorVisible, true);
    assert.equal(terminalError.value.events.some((event) => event.name === "cairn-error" && event.detail.kind === "service"), true);
    assertGeometry(terminalError.value.geometry);
    assert.equal(terminalError.value.geometry.newestVisible, true);
    assertNormalTextContrast(terminalError.value.ready.contrasts);
    assertHostEvents(terminalError.value.events);

    const desktop = await scenario("desktop", { width: 1280, height: 800 });
    const desktopPosts = desktop.api.filter((item) => item.method === "POST");
    assert.equal(desktopPosts.length, 2);
    assert.deepEqual(Object.keys(JSON.parse(desktopPosts[0].body)).sort(), ["history", "message", "session_id"]);
    assert.deepEqual(JSON.parse(desktopPosts[0].body).history, []);
    assert.equal(JSON.parse(desktopPosts[1].body).history.length, 2);
    assert.equal(desktop.value.citation.target, "_blank");
    assert.equal(desktop.value.citation.rel, "noopener noreferrer");
    assert.equal(desktop.value.citation.referrerPolicy, "no-referrer");
    assert.equal(desktop.value.cleared, true);
    assert.equal(desktop.value.sessionRotated, true);
    assert.equal(desktop.value.canaryRetained, false);
    assert.deepEqual(desktop.value.storageKeys, []);
    assert.equal(desktop.value.nonceChange.style, "Q0hBTkdFRC1OT05DRQ==");
    assert.equal(desktop.value.nonceChange.configuration.includes("Q0hBTkdFRC1OT05DRQ=="), false);
    assert.deepEqual(desktop.value.nonceChange.mirrors, []);
    assert.equal(desktop.value.clearedNonce.style, "");
    assert.equal(desktop.value.clearedNonce.configuration.includes("Q0hBTkdFRC1OT05DRQ=="), false);
    assert.deepEqual(desktop.value.clearedNonce.mirrors, []);
    assertGeometry(desktop.value.geometry);
    assert.equal(desktop.value.geometry.newestVisible, true);
    assertHostEvents(desktop.value.events);

    const csp = await scenario("csp", { width: 640, height: 400 });
    assert.deepEqual(csp.value.violations, []);
    assert.equal(csp.value.styleNonce, NONCE);
    assert.notEqual(csp.value.styleAttribute, NONCE);
    assert.equal(csp.value.hostNonce, NONCE);
    assert.equal(csp.value.hostAttribute, "");
    assert.equal(csp.value.configurationBefore.includes(NONCE), false);
    assert.deepEqual(csp.value.mirrorsBefore, []);
    assert.equal(csp.value.invalidConfiguration, true);
    assert.equal(csp.value.recoveredConfiguration.includes(NONCE), false);
    assert.equal(csp.value.recoveredStyleNonce, NONCE);
    assert.equal(csp.value.afterClearConfiguration.includes(NONCE), false);
    assert.deepEqual(csp.value.mirrorsAfter, []);
    assert.equal(csp.value.styled, true);
    assertGeometry(csp.value.geometry);
    assertHostEvents(csp.value.events);
    const cspNegative = await scenario("csp-negative", { width: 640, height: 400 });
    assert.equal(cspNegative.value.violations.some((item) => item.directive === "style-src-elem"), true);
    assert.equal(cspNegative.value.styled, false);

    for (const name of ["mismatch", "cors"]) {
      const refused = await scenario(name, { width: 640, height: 400 });
      assert.equal(refused.value.sendDisabled, true);
      assert.equal(refused.api.some((item) => item.method === "POST"), false);
      assert.equal(refused.value.events.some((event) => event.name === "cairn-error"), true);
      assert.equal(JSON.stringify(refused.value).includes(CANARY), false);
      assertHostEvents(refused.value.events);
    }

    const keyboard = await scenario("keyboard", { width: 390, height: 844 }, {
      mediaFeatures: [
        { name: "prefers-reduced-motion", value: "reduce" },
        { name: "forced-colors", value: "active" },
      ],
    });
    assert.equal(keyboard.value.closed, true);
    assert.equal(keyboard.value.launcherFocused, true, JSON.stringify(keyboard.value));
    assert.equal(keyboard.value.events.some((event) => event.name === "cairn-close" && event.detail.reason === "escape"), true);
    assert.notEqual(keyboard.value.forced, "none");
    assertHostEvents(keyboard.value.events);

    const insets = { top: 20, right: 16, bottom: 24, left: 12 };
    const safe = await scenario("safe", { width: 360, height: 640 }, { safeAreaInsets: insets });
    assert.match(safe.safeAreaNegativeControl, /setSafeAreaInsets.*wasn't found/u);
    const usable = { top: insets.top, left: insets.left, right: 360 - insets.right, bottom: 640 - insets.bottom };
    assertGeometry(safe.value.geometry);
    assert.equal(safe.value.geometry.panel.top >= usable.top - 0.5, true, JSON.stringify(safe.value));
    assert.equal(safe.value.geometry.panel.right <= usable.right + 0.5, true);
    assert.equal(safe.value.closedLauncher.right <= usable.right + 0.5, true);
    assert.equal(safe.value.closedLauncher.bottom <= usable.bottom + 0.5, true);
    assertHostEvents(safe.value.events);
    console.log(`widget production browser matrix passed: ${JSON.stringify({ mobile: mobile.value.geometry, refusal: refusal.value.geometry, error: terminalError.value.geometry, desktop: desktop.value.geometry, csp: csp.value.geometry, keyboard: keyboard.value.geometry, safe: safe.value.geometry, contrasts: { mobile: mobile.value.ready.contrasts, light: refusal.value.ready.contrasts, dark: terminalError.value.ready.contrasts }, browser: safe.product, protocol: safe.protocolVersion })}`);
  });

  for (const name of ["mobile", "refusal", "error", "desktop", "keyboard", "safe", "mismatch", "cors"]) {
    assert.equal(siteMarkers.includes(name), true, `${name} must mark connected state before opening`);
    const requests = apiRequests.filter((item) => item.scenario === name);
    const markerIndex = siteMarkers.indexOf(name);
    assert.ok(markerIndex >= 0);
    assert.equal(requests.filter((item) => item.url === "/api/v1/capabilities").length, 1);
    assert.equal(requests.filter((item) => item.url === "/api/v1/capabilities").every((item) => item.origin === siteOrigin), true);
    for (const request of requests.filter((item) => item.url === "/api/v1/capabilities")) {
      assert.equal(request.method, "GET");
      assert.equal(request.accept, "application/json");
      assert.equal(request.cookie, null);
      assert.equal(request.authorization, null);
      assert.equal(request.referer, null);
    }
    for (const request of requests.filter((item) => item.method === "POST")) {
      assert.equal(request.accept, "text/event-stream");
      assert.equal(request.contentType, "application/json");
      assert.equal(request.cookie, null);
      assert.equal(request.authorization, null);
      assert.equal(request.referer, null);
    }
  }
} finally {
  dnsGuard.restore();
  await siteServer.close();
  await apiServer.close();
}
