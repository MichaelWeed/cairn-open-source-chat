import assert from "node:assert/strict";
import dns from "node:dns";
import net from "node:net";
import { access, readFile } from "node:fs/promises";

import {
  browserPath,
  installDnsGuards,
  installSocketGuards,
  runBrowserScenario,
  startLoopbackServer,
  verifyHarnessContracts,
  withMatrixDeadline,
} from "./browser-harness.mjs";

const NONCE = "YWJjZGVmZ2hpamtsbW5vcA==";
const CONTENT_CANARY = ["PRIVATE", "CONTENT", "CANARY"].join("-");
const indexTemplate = await readFile(new URL("../../examples/neutral-site/index.html", import.meta.url), "utf8");
const styles = await readFile(new URL("../../examples/neutral-site/styles.css", import.meta.url), "utf8");
const widgetBytes = await readFile(new URL("../../backend/app/static/widget/widget.js", import.meta.url));
const capabilities = JSON.parse(await readFile(new URL("../../backend/app/capabilities.json", import.meta.url), "utf8"));
assert.equal(capabilities.schema_version, "1.2");
assert.equal(capabilities.compatibility.widget, "0.2.0");

const browser = await browserPath();
const requests = [];
let currentScenario = "";
let siteOrigin = "";

function responseCors(origin) {
  return currentScenario === "disallowed-origin"
    ? { "access-control-allow-origin": "http://127.0.0.1:1" }
    : { "access-control-allow-origin": origin, vary: "Origin" };
}

function sse(name, payload) {
  return `event: ${name}\ndata: ${JSON.stringify(payload)}\n\n`;
}

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString("utf8");
}

const apiServer = await startLoopbackServer(async (request, response) => {
  const body = request.method === "POST" ? await readBody(request) : "";
  requests.push({ scenario: currentScenario, method: request.method, url: request.url, origin: request.headers.origin ?? null, cookie: request.headers.cookie ?? null, authorization: request.headers.authorization ?? null, referer: request.headers.referer ?? null, body });
  if (request.url === "/widget/widget.js") {
    const source = currentScenario === "old-padding" ? widgetBytes.toString("utf8").replace("padding: 0 1rem", "padding: 1rem") : widgetBytes;
    if (currentScenario === "old-padding") assert.notDeepEqual(Buffer.from(source), widgetBytes);
    response.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
    response.end(source);
    return;
  }
  const cors = responseCors(request.headers.origin ?? "");
  if (request.url === "/api/v1/capabilities") {
    const manifest = ["geometry", "old-padding"].includes(currentScenario) ? { ...capabilities, compatibility: { ...capabilities.compatibility, widget: "0.1.0" } } : capabilities;
    response.writeHead(200, { "content-type": "application/json", ...cors });
    response.end(JSON.stringify(manifest));
    return;
  }
  if (request.url === "/api/v1/chat/message" && request.method === "OPTIONS") {
    response.writeHead(204, { ...cors, "access-control-allow-methods": "POST", "access-control-allow-headers": "content-type" });
    response.end();
    return;
  }
  if (request.url === "/api/v1/chat/message" && request.method === "POST") {
    response.writeHead(200, { "content-type": "text/event-stream; charset=utf-8", ...cors });
    if (currentScenario === "refusal" || currentScenario === "keyboard-handoff") {
      response.end(sse("status", { type: "status", state: "refusing", label: "No confident match found" }) + sse("chunk", { type: "chunk", delta: "I do not have a supported answer." }) + sse("done", { type: "done", finish_reason: "refused" }));
    } else if (currentScenario === "error") {
      response.end(sse("error", { type: "error", code: "provider_unavailable", message: "The service is unavailable.", retryable: false }));
    } else {
      const postCount = requests.filter((item) => item.scenario === currentScenario && item.method === "POST").length;
      response.end(sse("status", { type: "status", state: "retrieving", label: "Searching the knowledge base" }) + (postCount === 1 ? sse("citations", { type: "citations", sources: [{ id: "source-1", title: "Shipping information", url: `${siteOrigin}/source` }] }) : "") + sse("chunk", { type: "chunk", delta: postCount === 1 ? "A grounded answer." : "A follow-up answer." }) + sse("done", { type: "done", finish_reason: "stop" }));
    }
    return;
  }
  response.writeHead(404, { "content-type": "text/plain", ...cors });
  response.end("not found");
});

function clientScript(scenario) {
  return `<script nonce="${NONCE}">(async()=>{try{
    const scenario=${JSON.stringify(scenario)},host=document.querySelector("cairn-chat"),result=document.querySelector("#result"),violations=[],factoryCalls=[];
    for(const name of ["createProvider","createHostedStore"]){Object.defineProperty(globalThis,name,{configurable:false,value:()=>{factoryCalls.push(name);throw new Error("application factory denied")}});}
    addEventListener("securitypolicyviolation",event=>violations.push({directive:event.violatedDirective,blocked:event.blockedURI}));
    const waitFor=async predicate=>{const deadline=performance.now()+5000;while(!predicate()){if(performance.now()>deadline)throw new Error("fixture deadline");await new Promise(resolve=>setTimeout(resolve,20));}};
    await customElements.whenDefined("cairn-chat");
    await waitFor(()=>[...document.styleSheets].some(sheet=>sheet.href?.endsWith("/styles.css")));
    const root=host.shadowRoot,launcher=root.querySelector(".launcher"),rect=element=>{const box=element.getBoundingClientRect();return{top:box.top,right:box.right,bottom:box.bottom,left:box.left,width:box.width,height:box.height}},inside=(a,b)=>a.top>=b.top-.5&&a.left>=b.left-.5&&a.right<=b.right+.5&&a.bottom<=b.bottom+.5;
    const rgb=value=>{const match=value.match(/[\\d.]+/gu);if(!match)throw new Error("unparsed color "+value);return match.slice(0,3).map(Number)},linear=value=>{value/=255;return value<=.04045?value/12.92:((value+.055)/1.055)**2.4},luminance=value=>{const [r,g,b]=rgb(value).map(linear);return .2126*r+.7152*g+.0722*b},contrast=(foreground,background)=>{const values=[luminance(getComputedStyle(foreground).color),luminance(getComputedStyle(background).backgroundColor)].sort((a,b)=>b-a);return(values[0]+.05)/(values[1]+.05)},colorContrast=(foreground,background)=>{const values=[luminance(foreground),luminance(background)].sort((a,b)=>b-a);return(values[0]+.05)/(values[1]+.05)};
    const skip=document.querySelector(".skip-link");if(scenario==="forced"){window.__cairnForcedReady=true;await waitFor(()=>window.__cairnForcedActivated===true);}else skip.focus();const skipStyle=getComputedStyle(skip),skipBoxFocused=rect(skip),skipFocusColor=skipStyle.outlineColor,skipFocusStyle=skipStyle.outlineStyle,skipVisible=(skipStyle.transform==="none"||skipBoxFocused.top>=0)&&document.activeElement===skip;
    if(scenario.startsWith("keyboard")){window.__cairnKeyboardReady=true;await waitFor(()=>window.__cairnKeyboardResult!==undefined);result.textContent=JSON.stringify(window.__cairnKeyboardResult);return;}
    if(scenario==="content-stress"){const paragraph=document.querySelector("section p");paragraph.dir="rtl";paragraph.textContent="مثال 中文 "+"UNBROKENTOKEN".repeat(80);}
    const closedLauncher=rect(launcher);
    launcher.click();
    if(["geometry","old-padding","disallowed-origin"].includes(scenario))await waitFor(()=>!root.querySelector(".error").hidden);else await waitFor(()=>!root.querySelector("textarea").disabled);
    const panel=root.querySelector(".panel"),messages=root.querySelector(".messages"),composer=root.querySelector("form"),panelBox=rect(panel),messagesBox=rect(messages),composerBox=rect(composer),viewport={top:0,left:0,right:innerWidth,bottom:innerHeight};
    const visible=[...root.querySelectorAll("button,a,textarea")].filter(element=>{const style=getComputedStyle(element);return !element.hidden&&element.getAttribute("aria-hidden")!=="true"&&style.display!=="none"&&style.visibility!=="hidden"&&style.opacity!=="0"&&element.getClientRects().length>0});
    const controlsReachable=visible.every(element=>{if(inside(rect(element),panelBox))return true;const scroller=element.closest(".tail")??element.closest(".messages");if(!scroller)return false;const before=scroller.scrollTop;element.scrollIntoView({block:"nearest"});const reachable=inside(rect(element),panelBox);scroller.scrollTop=before;return reachable});
    const geometry={panel:panelBox,messages:messagesBox,composer:composerBox,closedLauncher,overlapPixels:Math.max(0,messagesBox.bottom-composerBox.top),messagesDoNotOverlapComposer:messagesBox.bottom<=composerBox.top+.5,panelInside:inside(panelBox,viewport),controlsInside:controlsReachable,targets44:visible.every(element=>{const box=rect(element);return box.width>=44&&box.height>=44}),noHorizontalOverflow:document.documentElement.scrollWidth<=innerWidth&&panel.scrollWidth<=panel.clientWidth};
    const order=[...root.querySelectorAll("button,textarea,a")].filter(element=>!element.hidden).map(element=>element.getAttribute("aria-label")||element.textContent.trim()||element.tagName);
    let exchange=null;
    if(["happy","refusal","error","content-stress","zoom","light","dark","forced","safe"].includes(scenario)){
      const input=root.querySelector("textarea");input.value=${JSON.stringify(CONTENT_CANARY)};input.dispatchEvent(new Event("input",{bubbles:true}));root.querySelector("form").requestSubmit();await waitFor(()=>root.querySelector(".messages").getAttribute("aria-busy")==="false");const citation=root.querySelector(".citation");
      if(scenario==="happy"){input.value="Follow-up";input.dispatchEvent(new Event("input",{bubbles:true}));root.querySelector("form").requestSubmit();await waitFor(()=>root.querySelectorAll("article").length===4&&root.querySelector(".messages").getAttribute("aria-busy")==="false");}
      exchange={articles:root.querySelectorAll("article").length,citation:citation?{text:citation.textContent,href:citation.href,target:citation.target,rel:citation.rel,referrerPolicy:citation.referrerPolicy}:null,handoffHidden:root.querySelector(".handoff").hidden,errorHidden:root.querySelector(".error").hidden};
    }
    const input=root.querySelector("textarea");input.focus();const inputFocus=getComputedStyle(input);input.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true,composed:true}));await Promise.resolve();
    const focusReturn=document.activeElement===host&&root.activeElement===launcher,hostText=[...document.querySelectorAll("header,main,footer")].every(element=>{const style=getComputedStyle(element);return style.visibility!=="hidden"&&style.display!=="none"});
    const hostBackground=getComputedStyle(document.documentElement).backgroundColor,usable=scenario==="safe"?{top:20,left:12,right:innerWidth-16,bottom:innerHeight-24}:viewport,visual={top:visualViewport.offsetTop,left:visualViewport.offsetLeft,right:visualViewport.offsetLeft+visualViewport.width,bottom:visualViewport.offsetTop+visualViewport.height};
    result.textContent=JSON.stringify({scenario,violations,factoryCalls,skipVisible,skipBoxFocused,skipTransform:skipStyle.transform,skipFocused:document.activeElement===skip,landmarks:{headers:document.querySelectorAll("body>header").length,mains:document.querySelectorAll("main").length,footers:document.querySelectorAll("body>footer").length,h1:document.querySelectorAll("h1").length,h2:document.querySelectorAll("h2").length},dialog:{role:panel.getAttribute("role"),ariaModal:panel.getAttribute("aria-modal"),labelledby:panel.getAttribute("aria-labelledby"),logRole:messages.getAttribute("role"),logLabel:messages.getAttribute("aria-label"),launcherLabel:launcher.getAttribute("aria-label"),inputLabel:root.querySelector("label").textContent,closeLabel:root.querySelector(".close").getAttribute("aria-label")},geometry,order,exchange,focus:{inputOutline:inputFocus.outlineStyle,inputOutlineWidth:inputFocus.outlineWidth,returned:focusReturn,skipStyle:skipFocusStyle},contrasts:{hostText:contrast(document.querySelector("main p"),document.documentElement),hostFocus:colorContrast(skipFocusColor,hostBackground),widgetLabel:contrast(root.querySelector("label"),panel),widgetDisclosure:contrast(root.querySelector(".disclosure"),panel),widgetInput:contrast(input,input),widgetSend:contrast(root.querySelector(".send"),root.querySelector(".send"))},hostTarget44:skipBoxFocused.width>=44&&skipBoxFocused.height>=44,safeInside:inside(panelBox,usable)&&inside(closedLauncher,usable),hostWidgetNoCollision:skipBoxFocused.right<=closedLauncher.left+.5||skipBoxFocused.bottom<=closedLauncher.top+.5,zoomScale:visualViewport.scale,devicePixelRatio,cssViewport:{width:innerWidth,height:innerHeight},zoomVisible:scenario!=="zoom"||inside(panelBox,visual),hostText,hostOverflow:document.documentElement.scrollWidth<=innerWidth,contentStress:document.querySelector("main").scrollWidth<=document.querySelector("main").clientWidth,contentDirection:getComputedStyle(document.querySelector("section p")).direction,reduced:getComputedStyle(launcher).transitionDuration,colors:{text:getComputedStyle(document.body).color,background:hostBackground,focus:skipFocusStyle},storage:{local:Object.keys(localStorage),session:Object.keys(sessionStorage)},styled:getComputedStyle(host).position==="fixed"});
  }catch(error){document.querySelector("#result").textContent=JSON.stringify({fixtureError:String(error)});}})();</script>`;
}

function page(scenario) {
  let html = indexTemplate.replaceAll("https://cairn.example", apiServer.origin).replaceAll("https://example.test", siteOrigin).replaceAll("REPLACE_WITH_RESPONSE_NONCE", NONCE);
  if (scenario === "missing-nonce") html = html.replace(` nonce="${NONCE}"></cairn-chat>`, " nonce=\"\"></cairn-chat>");
  return html.replace("</body>", `<pre id="result" hidden></pre>${clientScript(scenario)}</body>`);
}

const siteServer = await startLoopbackServer((request, response) => {
  if (request.url === "/styles.css") {
    response.writeHead(200, { "content-type": "text/css; charset=utf-8" });
    response.end(styles);
  } else if (request.url === "/unexpected") {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end("<!doctype html><img src=https://blocked.invalid/pixel><p>never ready</p>");
  } else if (request.url === "/server-failure") {
    throw new Error("synthetic server failure");
  } else {
    response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    response.end(page(currentScenario));
  }
});
siteOrigin = siteServer.origin;

async function scenario(name, viewport, options = {}) {
  currentScenario = name;
  const before = requests.length;
  const output = await runBrowserScenario({ browser, url: `${siteOrigin}/?scenario=${name}`, viewport, allowedOrigins: [siteOrigin, apiServer.origin], ...options });
  assert.equal(output.value.fixtureError, undefined, `${name}: ${output.value.fixtureError}`);
  return { ...output, apiRequests: requests.slice(before) };
}

function assertBase(result) {
  assert.deepEqual(result.landmarks, { headers: 1, mains: 1, footers: 1, h1: 1, h2: 3 });
  assert.equal(result.skipVisible, true, JSON.stringify(result));
  assert.equal(result.dialog.role, "dialog");
  assert.equal(result.dialog.ariaModal, "false");
  assert.ok(result.dialog.labelledby);
  assert.equal(result.dialog.logRole, "log");
  assert.equal(result.dialog.logLabel, "Conversation with Example Support");
  assert.equal(result.dialog.launcherLabel, "Chat with Example Support");
  assert.equal(result.dialog.inputLabel, "Message");
  assert.equal(result.dialog.closeLabel, "Close chat");
  assert.equal(result.geometry.panelInside, true, JSON.stringify(result));
  assert.equal(result.geometry.controlsInside, true, JSON.stringify(result));
  assert.equal(result.geometry.targets44, true, JSON.stringify(result));
  assert.equal(result.geometry.noHorizontalOverflow, true, JSON.stringify(result));
  assert.equal(result.geometry.messagesDoNotOverlapComposer, true, JSON.stringify(result));
  assert.equal(result.hostOverflow, true);
  assert.equal(result.hostText, true);
  assert.equal(result.contentStress, true);
  assert.equal(result.focus.returned, true);
  assert.notEqual(result.focus.skipStyle, "none");
  assert.equal(result.hostTarget44, true);
  assert.equal(result.hostWidgetNoCollision, true);
  for (const [name, ratio] of Object.entries(result.contrasts)) assert.equal(ratio >= (name === "hostFocus" ? 3 : 4.5), true, `${name} contrast ${ratio}`);
  assert.deepEqual(result.storage.local, []);
  assert.deepEqual(result.storage.session, ["cairn-chat-session-id"]);
  assert.deepEqual(result.factoryCalls, []);
  assert.equal(result.styled, true);
}

const dnsGuard = installDnsGuards();
const socketGuard = installSocketGuards();
try {
  await dnsGuard.verify();
  socketGuard.verify();
  await verifyHarnessContracts();
  await withMatrixDeadline(async (matrixSignal) => {
    const matrix = [];
    let lightColors;
    let darkColors;
    for (const [name, viewport, options] of [
      ["happy", { width: 360, height: 640 }, {}],
      ["content-stress", { width: 768, height: 1024 }, {}],
      ["light", { width: 1440, height: 900 }, { mediaFeatures: [{ name: "prefers-color-scheme", value: "light" }] }],
      ["dark", { width: 768, height: 1024 }, { mediaFeatures: [{ name: "prefers-color-scheme", value: "dark" }] }],
      ["zoom", { width: 384, height: 512 }, { deviceScaleFactor: 2, physicalViewport: { width: 768, height: 1024 } }],
      ["forced", { width: 360, height: 640 }, { mediaFeatures: [{ name: "prefers-reduced-motion", value: "reduce" }, { name: "forced-colors", value: "active" }], interact: async ({ evaluate, key }) => { await evaluate("new Promise(resolve=>{const check=()=>window.__cairnForcedReady?resolve():setTimeout(check,10);check()})", true); let focused = false; for (let attempt = 0; attempt < 3 && !focused; attempt += 1) { await key("Tab"); focused = (await evaluate("document.activeElement?.classList.contains('skip-link')===true")).result.value; } assert.equal(focused, true, "real Tab traversal must reach the forced-colors skip link"); await evaluate("window.__cairnForcedActivated=true"); } }],
      ["safe", { width: 360, height: 640 }, { safeAreaInsets: { top: 20, right: 16, bottom: 24, left: 12 } }],
      ["refusal", { width: 360, height: 640 }, {}],
      ["error", { width: 360, height: 640 }, {}],
    ]) {
      matrixSignal.throwIfAborted();
      const output = await scenario(name, viewport, { ...options, signal: matrixSignal });
      assertBase(output.value);
      assert.deepEqual(output.value.violations, []);
      for (const request of output.apiRequests) {
        assert.equal(request.origin, request.url === "/widget/widget.js" ? null : siteOrigin);
        assert.equal(request.cookie, null);
        assert.equal(request.authorization, null);
        assert.equal(request.referer, null);
      }
      matrix.push({ name, geometry: output.value.geometry, colors: output.value.colors });
      if (name === "light") lightColors = output.value.colors;
      if (name === "dark") darkColors = output.value.colors;
      if (name === "content-stress") assert.equal(output.value.contentDirection, "rtl");
      if (name === "happy") {
        const posts = output.apiRequests.filter((item) => item.method === "POST");
        assert.equal(posts.length, 2);
        assert.deepEqual(JSON.parse(posts[0].body).history, []);
        assert.equal(JSON.parse(posts[1].body).history.length, 2);
        assert.equal(output.value.exchange.articles, 4);
        assert.deepEqual(output.value.exchange.citation, { text: "Shipping information", href: `${siteOrigin}/source`, target: "_blank", rel: "noopener noreferrer", referrerPolicy: "no-referrer" });
        assert.deepEqual(output.value.order, ["Chat with Example Support", "Close chat", "TEXTAREA", "Clear chat", "Send", "Privacy details"]);
      }
      if (name === "refusal") assert.equal(output.value.exchange.handoffHidden, false);
      if (name === "error") {
        assert.equal(output.value.exchange.errorHidden, false);
        assert.equal(output.value.exchange.handoffHidden, false);
      }
      if (name === "forced") {
        assert.equal(Number.parseFloat(output.value.reduced) <= 0.00001, true);
        assert.notEqual(output.value.colors.text, output.value.colors.background);
        assert.notEqual(output.value.colors.focus, "none");
      }
      if (name === "safe") assert.match(output.safeAreaNegativeControl, /setSafeAreaInsets.*wasn't found/u);
      if (name === "safe") assert.equal(output.value.safeInside, true, JSON.stringify(output.value));
      if (name === "zoom") assert.deepEqual({ scale: output.value.devicePixelRatio, physicalWidth: output.value.cssViewport.width * output.value.devicePixelRatio, physicalHeight: output.value.cssViewport.height * output.value.devicePixelRatio }, { scale: 2, physicalWidth: 768, physicalHeight: 1024 });
      if (name === "zoom") assert.equal(output.value.zoomVisible, true, JSON.stringify(output.value));
    }
    assert.notDeepEqual(lightColors, darkColors);

    matrixSignal.throwIfAborted();
    const geometry = await scenario("geometry", { width: 640, height: 400 }, { signal: matrixSignal });
    assertBase(geometry.value);
    assert.equal(geometry.value.geometry.overlapPixels, 0);
    const oldPadding = await scenario("old-padding", { width: 640, height: 400 }, { signal: matrixSignal });
    assert.equal(oldPadding.value.geometry.overlapPixels, 31);
    assert.equal(oldPadding.value.geometry.messages.bottom <= oldPadding.value.geometry.composer.top + 0.5, false);

    const missingNonce = await scenario("missing-nonce", { width: 640, height: 400 }, { signal: matrixSignal });
    assert.equal(missingNonce.value.violations.some((item) => item.directive === "style-src-elem"), true);
    assert.equal(missingNonce.value.styled, false);
    const disallowed = await scenario("disallowed-origin", { width: 640, height: 400 }, { signal: matrixSignal });
    assert.equal(disallowed.apiRequests.some((item) => item.method === "POST"), false);

    const keyboard = await scenario("keyboard", { width: 768, height: 1024 }, {
      signal: matrixSignal,
      interact: async ({ evaluate, key, text }) => {
        await evaluate("new Promise(resolve=>{const check=()=>window.__cairnKeyboardReady?resolve():setTimeout(check,10);check()})", true);
        const active = async () => (await evaluate("(()=>{const outer=document.activeElement,inner=outer?.shadowRoot?.activeElement;return{outer:outer?.id||outer?.localName,inner:inner?.className||inner?.id||null,focusVisible:(inner??outer)?.matches(':focus-visible')??false}})()" )).result.value;
        const states = [{ step: "skip", ...(await active()) }];
        await key("Enter");
        states.push({ step: "skip-activated", ...(await active()) });
        assert.equal(states.at(-1).outer, "main-content", JSON.stringify(states));
        await key("Tab");
        states.push({ step: "launcher", ...(await active()) });
        assert.deepEqual([states.at(-1).outer, states.at(-1).inner], ["cairn-chat", "launcher"], JSON.stringify(states));
        await key("Enter");
        const opened = (await evaluate("!document.querySelector('cairn-chat').shadowRoot.querySelector('.panel').hidden")).result.value;
        assert.equal(opened, true, JSON.stringify(states));
        await evaluate("new Promise(resolve=>{const check=()=>{const host=document.querySelector('cairn-chat'),input=host?.shadowRoot?.querySelector('textarea');if(input&&!input.disabled&&host.shadowRoot.activeElement===input)resolve();else setTimeout(check,10)};check()})", true);
        states.push({ step: "input", ...(await active()) });
        await text("Where is my order?");
        await key("Enter");
        await evaluate("new Promise(resolve=>{const check=()=>document.querySelector('cairn-chat').shadowRoot.querySelector('article[data-complete=true]')?resolve():setTimeout(check,10);check()})", true);
        await evaluate("(()=>{const host=document.querySelector('cairn-chat'),root=host.shadowRoot;window.__keyboardActivations={citation:false,clear:false,send:false,privacy:false};root.querySelector('.citation').addEventListener('click',event=>{event.preventDefault();window.__keyboardActivations.citation=true},{once:true});root.querySelector('.send').addEventListener('click',()=>{window.__keyboardActivations.send=true},{once:true});root.querySelector('.privacy').addEventListener('click',event=>{event.preventDefault();window.__keyboardActivations.privacy=true},{once:true});host.addEventListener('cairn-clear',()=>{window.__keyboardActivations.clear=true},{once:true})})()");
        await key("Tab", 8);
        states.push({ step: "citation", ...(await active()) });
        await key("Enter");
        await key("Tab");
        states.push({ step: "input-return", ...(await active()) });
        await key("Tab");
        states.push({ step: "clear", ...(await active()) });
        await key("Enter");
        states.push({ step: "clear-activated", ...(await active()) });
        await text("Please check again.");
        await key("Tab");
        states.push({ step: "clear-return", ...(await active()) });
        await key("Tab");
        states.push({ step: "send", ...(await active()) });
        await key("Enter");
        await evaluate("new Promise(resolve=>{const check=()=>document.querySelector('cairn-chat').shadowRoot.querySelector('article[data-complete=true]')?resolve():setTimeout(check,10);check()})", true);
        states.push({ step: "send-activated", ...(await active()) });
        await key("Tab");
        states.push({ step: "privacy", ...(await active()) });
        await key("Enter");
        await key("Tab");
        states.push({ step: "tab-leaves-dialog", ...(await active()) });
        await key("Tab", 8);
        states.push({ step: "shift-tab-return", ...(await active()) });
        await key("Escape");
        states.push({ step: "escape-return", ...(await active()) });
        const activations = (await evaluate("window.__keyboardActivations")).result.value;
        await evaluate(`window.__cairnKeyboardResult=${JSON.stringify({ states, activations })}`);
      },
    });
    assert.deepEqual(keyboard.value.states.map((item) => [item.step, item.outer, item.inner?.replace(/^cairn-chat-\d+-input$/u, "input") ?? null]), [
      ["skip", "a", null],
      ["skip-activated", "main-content", null],
      ["launcher", "cairn-chat", "launcher"],
      ["input", "cairn-chat", "input"],
      ["citation", "cairn-chat", "citation"],
      ["input-return", "cairn-chat", "input"],
      ["clear", "cairn-chat", "clear"],
      ["clear-activated", "cairn-chat", "input"],
      ["clear-return", "cairn-chat", "clear"],
      ["send", "cairn-chat", "send"],
      ["send-activated", "body", null],
      ["privacy", "cairn-chat", "privacy"],
      ["tab-leaves-dialog", "body", null],
      ["shift-tab-return", "cairn-chat", "privacy"],
      ["escape-return", "cairn-chat", "launcher"],
    ]);
    assert.equal(keyboard.value.states.filter((item) => !["send-activated", "tab-leaves-dialog"].includes(item.step)).every((item) => item.focusVisible), true, JSON.stringify(keyboard.value.states));
    assert.deepEqual(keyboard.value.activations, { citation: true, clear: true, send: true, privacy: true });
    assert.equal(keyboard.apiRequests.filter((item) => item.method === "POST" && item.url === "/api/v1/chat/message").length, 2);

    const keyboardHandoff = await scenario("keyboard-handoff", { width: 768, height: 1024 }, {
      signal: matrixSignal,
      interact: async ({ evaluate, key, text }) => {
        await evaluate("new Promise(resolve=>{const check=()=>window.__cairnKeyboardReady?resolve():setTimeout(check,10);check()})", true);
        await key("Enter");
        await key("Tab");
        await key("Enter");
        await evaluate("new Promise(resolve=>{const check=()=>{const host=document.querySelector('cairn-chat'),input=host?.shadowRoot?.querySelector('textarea');if(input&&!input.disabled&&host.shadowRoot.activeElement===input)resolve();else setTimeout(check,10)};check()})", true);
        await evaluate("document.querySelector('cairn-chat').addEventListener('cairn-handoff',event=>{event.preventDefault();window.__handoffActivated=true},{once:true})");
        await text("Please help");
        await key("Enter");
        await evaluate("new Promise(resolve=>{const check=()=>!document.querySelector('cairn-chat').shadowRoot.querySelector('.handoff').hidden?resolve():setTimeout(check,10);check()})", true);
        await key("Tab");
        await key("Tab");
        await key("Tab");
        const before = (await evaluate("(()=>{const root=document.querySelector('cairn-chat').shadowRoot,link=root.activeElement;return{className:link.className,label:link.textContent.trim(),href:link.href,target:link.target,rel:link.rel,referrerPolicy:link.referrerPolicy,focusVisible:link.matches(':focus-visible')}})()" )).result.value;
        await key("Enter");
        const activated = (await evaluate("window.__handoffActivated===true")).result.value;
        await evaluate(`window.__cairnKeyboardResult=${JSON.stringify({ before, activated })}`);
      },
    });
    assert.deepEqual(keyboardHandoff.value.before, { className: "handoff", label: "Contact support", href: `${siteOrigin}/support`, target: "_blank", rel: "noopener noreferrer", referrerPolicy: "no-referrer", focusVisible: true });
    assert.equal(keyboardHandoff.value.activated, true);

    const cleanupPaths = [];
    await assert.rejects(runBrowserScenario({ browser, url: `${siteOrigin}/`, viewport: { width: 360, height: 640 }, allowedOrigins: [siteOrigin], onTemporaryDirectory: (path) => { cleanupPaths.push(path); throw new Error("synthetic setup assertion"); } }), /synthetic setup assertion/u);
    await assert.rejects(runBrowserScenario({ browser, url: `${siteOrigin}/unexpected`, viewport: { width: 360, height: 640 }, allowedOrigins: [siteOrigin, apiServer.origin], timeoutMilliseconds: 10_000, onTemporaryDirectory: (path) => cleanupPaths.push(path) }), /unexpected requests/u);
    await assert.rejects(runBrowserScenario({ browser, url: `${siteOrigin}/`, viewport: { width: 360, height: 640 }, allowedOrigins: [siteOrigin, apiServer.origin], timeoutMilliseconds: 5_000, interact: () => new Promise(() => undefined), onTemporaryDirectory: (path) => cleanupPaths.push(path) }), /absolute deadline during interaction/u);
    await assert.rejects(runBrowserScenario({ browser, url: `${siteOrigin}/server-failure`, viewport: { width: 360, height: 640 }, allowedOrigins: [siteOrigin], timeoutMilliseconds: 5_000, onTemporaryDirectory: (path) => cleanupPaths.push(path) }), /timed out/u);
    await assert.rejects(runBrowserScenario({ browser: "/definitely/missing/cairn-browser", url: `${siteOrigin}/`, viewport: { width: 360, height: 640 }, allowedOrigins: [siteOrigin], timeoutMilliseconds: 250, onTemporaryDirectory: (path) => cleanupPaths.push(path) }));
    for (const path of cleanupPaths) await assert.rejects(access(path));
    assert.equal(net.connect.name, "guardedConnect");
    assert.notEqual(dns.lookup.name, "lookup");
    console.log(`neutral example browser matrix passed: ${JSON.stringify({ browser: geometry.product, protocol: geometry.protocolVersion, geometry: geometry.value.geometry, oldPadding: oldPadding.value.geometry, matrix })}`);
  }, 300_000);
} finally {
  socketGuard.restore();
  dnsGuard.restore();
  await siteServer.close();
  await apiServer.close();
}
