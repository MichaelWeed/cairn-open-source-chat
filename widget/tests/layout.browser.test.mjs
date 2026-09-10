import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { build } from "esbuild";
import {
  browserPath,
  removeTemporaryDirectory,
  runBrowserScenario,
  verifyHarnessContracts,
} from "./browser-harness.mjs";

const viewport = { width: 640, height: 400 };

function fixture(widgetSource) {
  const safeWidgetSource = widgetSource.replace(/<\/script/gi, "<\\/script");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>html,body{margin:0}</style></head><body><cairn-chat api-url="invalid-fixture-url"></cairn-chat><pre id="result"></pre><script>${safeWidgetSource}</script><script>
    const host=document.querySelector("cairn-chat");const root=host.shadowRoot;root.querySelector(".launcher").click();
    const rectangle=(selector)=>{const b=root.querySelector(selector).getBoundingClientRect();return{top:b.top,right:b.right,bottom:b.bottom,left:b.left,width:b.width,height:b.height}};
    const panel=rectangle(".panel"),messages=rectangle(".messages"),composer=rectangle("form"),error=rectangle(".error"),controls=[rectangle("textarea"),rectangle(".send"),rectangle(".close")],tolerance=.5,viewportBounds={top:0,right:innerWidth,bottom:innerHeight,left:0};
    const containedBy=(inner,outer)=>inner.top>=outer.top-tolerance&&inner.left>=outer.left-tolerance&&inner.right<=outer.right+tolerance&&inner.bottom<=outer.bottom+tolerance;
    const errorElement=root.querySelector(".error"),errorStyle=getComputedStyle(errorElement);
    const result={viewport:{width:innerWidth,height:innerHeight},panel,messages,composer,error,controls,overlapPixels:Math.max(0,messages.bottom-composer.top),panelInsideViewport:containedBy(panel,viewportBounds),controlsInsideViewport:controls.every((control)=>containedBy(control,viewportBounds)),controlsInsidePanel:controls.every((control)=>containedBy(control,panel)),errorInsideViewport:containedBy(error,viewportBounds),errorInsidePanel:containedBy(error,panel),messagesDoNotOverlapComposer:messages.bottom<=composer.top+tolerance,errorVisible:!errorElement.hidden&&errorStyle.display!=="none"&&errorStyle.visibility!=="hidden"&&Number(errorStyle.opacity)>0&&error.width>tolerance&&error.height>tolerance};
    document.querySelector("#result").textContent=JSON.stringify(result);
  </script></body></html>`;
}

await verifyHarnessContracts();
const browser = await browserPath();
const bundle = await build({
  entryPoints: [new URL("../src/index.ts", import.meta.url).pathname],
  bundle: true,
  minify: true,
  write: false,
});
const temporaryDirectory = await mkdtemp(join(tmpdir(), "cairn-widget-layout-"));
try {
  const positivePath = join(temporaryDirectory, "fixture.html");
  await writeFile(positivePath, fixture(bundle.outputFiles[0].text), "utf8");
  const { value: result } = await runBrowserScenario({
    browser,
    url: pathToFileURL(positivePath).href,
    viewport,
    allowedOrigins: ["null"],
  });
  assert.deepEqual(result.viewport, viewport);
  assert.equal(result.errorVisible, true);
  assert.equal(result.panelInsideViewport, true);
  assert.equal(result.controlsInsideViewport, true);
  assert.equal(result.controlsInsidePanel, true);
  assert.equal(result.errorInsideViewport, true);
  assert.equal(result.errorInsidePanel, true);
  assert.equal(result.messagesDoNotOverlapComposer, true, `message region overlaps composer by ${result.overlapPixels}px`);

  const oldPaddingBundle = bundle.outputFiles[0].text.replace("padding: 0 1rem", "padding: 1rem");
  assert.notEqual(oldPaddingBundle, bundle.outputFiles[0].text);
  const negativePath = join(temporaryDirectory, "old-padding.html");
  await writeFile(negativePath, fixture(oldPaddingBundle), "utf8");
  const { value: negative } = await runBrowserScenario({
    browser,
    url: pathToFileURL(negativePath).href,
    viewport,
    allowedOrigins: ["null"],
  });
  assert.equal(negative.messagesDoNotOverlapComposer, false);
  console.log(`widget rendered layout test passed: ${JSON.stringify(result)}`);
  console.log(`widget old-padding negative control passed: ${JSON.stringify(negative)}`);
} finally {
  await removeTemporaryDirectory(temporaryDirectory);
}
