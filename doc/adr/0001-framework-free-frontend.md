# ADR 0001: Keep the framework-free TypeScript frontend

- **Status:** Accepted
- **Decision date:** 2026-09-04
- **Recorded and measured:** 2026-09-05
- **Scope:** Notes MVP frontend architecture, epic [#36](https://github.com/mvoronin/trellmark/issues/36)
- **Record request:** [#37](https://github.com/mvoronin/trellmark/issues/37)

## Context

Trellmark is a private, single-user knowledge organizer with working bookmark, authentication, Safe mode, hierarchy, metadata, and import/export behavior. The browser uses strict TypeScript and native DOM APIs. Its application entry combines UI state, server records, event handlers, DOM construction, and selectors for static HTML. The problem is concentrated ownership and a fragile HTML/handle seam; size alone does not prove a framework migration is necessary.

The original decision was made on **2026-09-04**. These are newly executed **2026-09-05 pre-extraction measurements**, not measurements retrospectively attributed to that decision date. Source revision: `8b27376fb9df178f955a9a7fee3f096eaf251153`. Measured at `2026-09-05T13:12:02.903Z`, using Node `v22.22.0` and the installed, locked TypeScript `7.0.2` native AST API. Plan 03-01's whole-tree build is already complete; application module extraction has not started.

| Metric | Result | Exact definition |
|---|---:|---|
| `web/src/app.ts` lines | 1,443 | Physical lines, including blank/comment lines; a terminal newline does not add an empty line |
| Top-level mutable bindings | 13 | Bound identifiers in direct source-file `let`/`var` variable statements; excludes nested declarations and `const`, even where a const-held object can mutate |
| Direct import-time `requiredElement` handles | 49 | Bound identifiers in direct source-file variable declarations whose initializer is a direct `requiredElement(...)` call |
| `document.createElement` call expressions | 32 | AST call expressions with that exact property-access receiver/name, at any depth; excludes comments, declarations, aliases, computed access, and `createElementNS` |
| `web/index.html` lines | 218 | Same physical-line definition |

The 13 mutable names are `groups`, `groupFilter`, `initialGroupsLoad`, `privateStateSerial`, `pendingImportFile`, `pendingImportRetryable`, `importRequestInFlight`, `importRequestSerial`, `activeDrag`, `editingGroup`, `editingUrl`, `urlEditRequestSerial`, and `urlEditRequestInFlight`. This is a binding count, not a claim that only 13 values in the program can change. The direct-handle count is not a whole-program effect analysis: it excludes selectors executed indirectly through other initialization functions.

The supplied discussion baseline of 35 `createElement` occurrences is historical and does not equal this call-expression metric. We report 32 for the defined measurement rather than carry the earlier number forward. None of these structural counts measures latency, memory consumption, defect rate, or framework performance.

### Reproduce the measurement

From the repository root with its existing locked dependencies installed, run the command below. It reads source through the pinned native compiler snapshot and closes both snapshot and API resources. It prints the current revision/date so a later run cannot silently present changed source as the historical baseline. To reproduce the exact baseline, use a separate checkout of the revision above; do not reset a working tree containing other work.

```sh
node --input-type=module <<'JS'
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { API } from "typescript/unstable/sync";
import * as ast from "typescript/unstable/ast";
import ts from "typescript";
const api = new API();
try {
  const snapshot = api.updateSnapshot({ openProjects: [path.resolve("tsconfig.json")] });
  try {
    const source = snapshot.getProjects()[0].program.getSourceFile(path.resolve("web/src/app.ts"));
    if (!source) throw new Error("Missing app.ts");
    const mutable = [];
    const handles = [];
    const collectNames = (name, output) => {
      if (ast.isIdentifier(name)) output.push(name.text);
      else for (const element of name.elements) {
        if (ast.isBindingElement(element)) collectNames(element.name, output);
      }
    };
    for (const statement of source.statements) {
      if (!ast.isVariableStatement(statement)) continue;
      for (const declaration of statement.declarationList.declarations) {
        if (!(statement.declarationList.flags & ast.NodeFlags.Const)) {
          collectNames(declaration.name, mutable);
        }
        const init = declaration.initializer;
        if (init && ast.isCallExpression(init) && ast.isIdentifier(init.expression)
            && init.expression.text === "requiredElement") collectNames(declaration.name, handles);
      }
    }
    let createElementCalls = 0;
    const visit = (node) => {
      if (ast.isCallExpression(node) && ast.isPropertyAccessExpression(node.expression)
          && ast.isIdentifier(node.expression.expression)
          && node.expression.expression.text === "document"
          && node.expression.name.text === "createElement") createElementCalls++;
      node.forEachChild(visit);
    };
    visit(source);
    const lines = (file) => {
      const text = fs.readFileSync(file, "utf8");
      return text === "" ? 0 : text.split("\n").length - Number(text.endsWith("\n"));
    };
    console.log(JSON.stringify({
      measuredAt: new Date().toISOString(),
      revision: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
      node: process.version, typescript: ts.version,
      appLines: lines("web/src/app.ts"), topLevelMutableBindings: mutable.length,
      directImportTimeRequiredElementHandles: handles.length,
      createElementCalls, indexLines: lines("web/index.html"),
      mutableNames: mutable
    }, null, 2));
  } finally { snapshot.dispose(); }
} finally { api.close(); }
JS
```

The `typescript/unstable/*` entrypoints are version-specific. This command was executed with the installed version above; a future compiler upgrade must revalidate the parser API rather than substitute guessed syntax. It adds no package or permanent measurement service.

## Decision

Keep the framework-free TypeScript SPA now. Retain strict compilation, browser ES modules with relative `.js` imports, committed generated browser output, and generated OpenAPI declarations behind the API adapter. Add no UI framework, bundler, runtime dependency, reactivity/diffing system, or JavaScript test runner in this phase.

Address the coupling with explicit state and markup owners, read-only rendering inputs, narrow shared primitives, and named composition entry points. Retain full re-rendering while preserving existing behavior, DOM structure, IDs, classes, ARIA attributes, and focus order. Shared static HTML may move into build-time fragments consumed by both the app and local design mode. The delivered page remains complete; no runtime fragment fetch is needed.

All children of epic #36 remain Phase 3 work. The source-tree build comes before module extraction. Notes editing, Markdown, and whole-product backup remain in their already assigned later phases.

## Alternatives

| Option | Benefits | Costs and reason for this decision |
|---|---|---|
| Retain native DOM and strict TypeScript | Preserves the existing runtime/build contract and DOM assertions; allows incremental ownership extraction with direct inspection of emitted modules | We must own state transitions, request lifetimes, rendering, handle validation, and component reuse. This is the selected near-term work, not a claim that native DOM eliminates complexity. |
| Adopt Svelte now | A component can colocate script, markup, and styles. Template bindings and component tooling reduce the separate-selector seam. This is the strongest alternative and preferred candidate if reconsidered. | Introduces component compilation and runtime integration, and requires a migration of the working UI. DOM/focus/lifecycle compatibility must be proved during that migration. We have not measured a Svelte prototype or quantified migration churn. |
| Adopt React now | Reusable components and colocated render logic can express ownership and shared state. Existing CSS can still be used. | Requires adopting its component/render model and toolchain integration while migrating the same working behavior. No demonstrated project need currently offsets that migration cost. The team's preference is Svelte's HTML/script/style component shape, not a claim that React cannot support this UI. |

Svelte's documented component format includes script, markup, and style sections. React documents components as JavaScript functions returning markup. These are capabilities of the alternatives, not comparative benchmarks. [Svelte component files](https://svelte.dev/docs/svelte/svelte-files), [React components](https://react.dev/learn/your-first-component)

## Strongest counterargument

The 218-line HTML document and 49 directly initialized handles form a real coordination burden: a selector, element type, and markup fragment must stay aligned across files. Svelte would put template bindings beside the script and styles that use them. That reduces accidental distance between a component's structure and behavior; explicit TypeScript modules alone do not provide the same integration.

Shared fragments and owner-scoped handle acquisition address the present duplication and ownership problem without replacing the runtime. They **do not provide compile-time component/template checking**. A generic `requiredElement<T>` annotation does not make the HTML file part of TypeScript's type system. Missing or mismatched markup still needs runtime validation and existing DOM/browser tests. We accept that residual cost rather than pretend fragment composition fully reproduces Svelte's benefits.

## Rationale and factual corrections

The current task is to expose boundaries while preserving a working product. Incremental native-module extraction lets the existing DOM-asserting suite remain the compatibility specification. A simultaneous framework migration would add another source of possible rendering, focus, and lifecycle change. That is a risk assessment, not a measured assertion that a framework necessarily changes every DOM node or forces test rewrites.

Two arguments in the original issue need correction:

- Svelte compilation does not imply zero browser runtime. Its compiler produces JavaScript modules, and the verified client transform adds imports from `svelte/internal/client`. Generated client code depends on runtime helpers. [Compiler API](https://svelte.dev/docs/svelte/svelte-compiler), [official client transform source](https://github.com/sveltejs/svelte/blob/main/packages/svelte/src/compiler/phases/3-transform/client/transform-client.js)
- Compiler upgrades can change emitted code, but this does not make committed output meaningless. TypeScript itself is compiled. Pinned inputs, compiler, options, and build environment make output reproducibility a checkable contract; the existing build compares paths and bytes. A Svelte pipeline would also need its own reproducibility checks. We have not installed Svelte or claimed a measured deterministic Svelte build here.

Official framework references were checked on **2026-09-05**; the linked `main` transform is a moving source reference, not a frozen release benchmark. The runtime import is directly visible in `client_component` and `client_module`.

If the decision changes, prefer **Svelte over React** because its colocated HTML/script/style component model fits the existing hand-written CSS and markup approach. This is a project-fit judgment. It does not rely on zero-runtime, smaller-bundle, faster-rendering, or migration-effort claims that have not been measured.

## Consequences

We accept responsibility for the native UI's explicit boundaries and verification:

- Finish the complete source-tree build and artifact contract before extraction: [#38](https://github.com/mvoronin/trellmark/issues/38).
- Separate API-owned records from UI state and make named operations own changes: [#39](https://github.com/mvoronin/trellmark/issues/39).
- Centralize request-ticket and in-flight handling so stale completions cannot restore private or outdated state: [#40](https://github.com/mvoronin/trellmark/issues/40).
- Introduce a typed, safe DOM-construction helper while retaining explicit SVG namespace handling: [#41](https://github.com/mvoronin/trellmark/issues/41).
- Extract shell capabilities and explicit composition, then Bookmarks model/view/public surfaces with markup handles owned locally: [#42](https://github.com/mvoronin/trellmark/issues/42), [#43](https://github.com/mvoronin/trellmark/issues/43).
- Enforce dependency direction and generated-contract ownership with local/CI checks: [#44](https://github.com/mvoronin/trellmark/issues/44).
- Reuse real side-effect-free views and shared HTML in local synthetic design mode: [#45](https://github.com/mvoronin/trellmark/issues/45).

These are Phase 3 deliverables, not work postponed to justify the decision. The ADR does not claim they are all implemented. Existing automated behavior/DOM, security, generated-artifact, and build checks remain authoritative. No screenshot baselines or automatic screenshot comparisons are introduced; manual appearance inspection happens after Phase 3 completion.

## Reconsideration

Revisit this decision if any of these observable symptoms appears:

1. Full re-rendering loses cursor position or scroll during Markdown editing.
2. A second routed view needs shared state.
3. Live search is measurably slow at realistic note counts.

Record a reproducible interaction for the first symptom, the actual shared-state ownership requirement for the second, and dataset size, hardware/browser, timing method, and observed latency for the third. These are prompts to compare options with evidence, not automatic authorization to migrate. They do not require implementing Notes, routing, or search in Phase 3, and there is no feature-count or line-count threshold.

[ADR index and record format](README.md)

## Delivered boundaries observed on 2026-09-05

The pre-extraction measurements above remain historical observations of
`8b27376fb9df178f955a9a7fee3f096eaf251153`; the removed `app.ts` is intentionally
referenced there. At implementation revision `41c141c`, application composition
lives in `web/src/main.ts`, with the sole local design composition exception in
`web/src/design/main.ts`. Shared primitives, the API adapter, shell and public
Bookmarks factories now have executable native TypeScript import rules.
Reusable factories acquire handles within their supplied root and expose
lifecycle disposal; importing views does not bootstrap authentication.

Complete HTML sources and shared fragments live in `web/html/`. Both generated
pages and the entire emitted module tree are checked without repairing drift.
`just design-mode` serves synthetic examples using real views, fragments and
styles; dedicated design assets are removed before entering runtime image
layers. These are implemented boundaries, not comparative framework performance
measurements. They do not remove the residual runtime HTML/handle validation
cost described above or change the reconsideration criteria.

The [final verification evidence](../../.planning/phases/03-frontend-boundaries-and-deterministic-build/03-14-EVIDENCE.md)
records the exact candidate, environment, regression and image checks. Manual
appearance inspection remains a user handoff after phase completion.
