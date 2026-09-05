const fs = require("node:fs");
const path = require("node:path");
const { fork } = require("node:child_process");
const { version } = require("typescript/package.json");

function owner(file) {
  const parts = file.split("/");
  if (file === "main.ts") return "main";
  if (file === "design/main.ts") return "design-entry";
  if (["shared", "api", "shell", "design", "generated"].includes(parts[0]) && parts.length > 1) return parts[0];
  if (parts[0] === "features" && parts.length > 2) return `feature:${parts[1]}`;
  return undefined;
}

function boundaryRule(from, to, target, typeOnly) {
  if (from === "generated") return "generated-no-imports";
  if (to === "generated" && from !== "api") return "generated-private";
  if ((to === "design" || to === "design-entry") && from !== "design" && from !== "design-entry") return "production-to-design";
  if (to === "main" || to === "design-entry") return "entry-private";
  if (from === "shared") return to === "shared" ? undefined : "shared-only";
  if (from === "api") return ["api", "shared", "generated"].includes(to) ? undefined : "api-direction";
  if (from === "shell") return ["shell", "shared", "api"].includes(to) ? undefined : "shell-to-feature";
  if (from?.startsWith("feature:")) {
    if (to === "shell") return "feature-to-shell";
    return [from, "shared", "api"].includes(to) ? undefined : "cross-feature";
  }
  const publicIndex = target === "shell/index.ts" || /^features\/[^/]+\/index\.ts$/.test(target);
  const publicView = target === "shell/view.ts" || /^features\/[^/]+\/view\.ts$/.test(target);
  if (from === "main") return publicIndex || ["shared", "api"].includes(to) ? undefined : "main-public-only";
  if (from === "design-entry") return publicIndex || publicView || ["shared", "design"].includes(to) || (to === "api" && typeOnly) ? undefined : "design-public-only";
  if (from === "design") return ["design", "shared"].includes(to) || (to === "api" && typeOnly) ? undefined : "design-isolated";
  return "unknown-owner";
}

async function inspectSnapshot({ root }) {
  root = fs.realpathSync(root);
  const sourceRoot = path.join(root, "web/src");
  const configPath = path.join(root, "tsconfig.json");
  const diagnostics = new Set();
  const relative = (file) => path.relative(root, file).split(path.sep).join("/");
  const report = (file, target, rule, detail = "") => {
    diagnostics.add(`${relative(file)} -> ${target} [${rule}]${detail ? ` ${detail}` : ""}`);
  };
  if (version !== "7.0.2" || !fs.existsSync(configPath)) {
    report(configPath, "<project>", "invalid-project", "Expected configured project and TypeScript 7.0.2");
    return [...diagnostics].sort();
  }
  const { API } = await import("typescript/unstable/sync");
  const ast = await import("typescript/unstable/ast");
  const api = new API({ cwd: root });
  let snapshot;
  try {
    snapshot = api.updateSnapshot({ openProjects: [configPath] });
    const project = snapshot.getProjects()[0];
    if (!project || project.rootFiles.length === 0) throw new Error("No configured sources");
    for (const diagnostic of project.program.getConfigFileParsingDiagnostics()) {
      report(diagnostic.fileName || configPath, "<project>", "invalid-project", diagnostic.text);
    }
    for (const diagnostic of project.program.getSyntacticDiagnostics()) {
      report(diagnostic.fileName || configPath, "<syntax>", "parse-error", diagnostic.text);
    }
    // Start with every configured root, then follow dependencies even when they
    // are excluded from root selection. Never rely on main's reachable graph.
    const pending = [...project.rootFiles];
    const visited = new Set();
    for (const file of pending) {
      if (visited.has(file)) continue;
      visited.add(file);
      const local = path.relative(sourceRoot, file).split(path.sep).join("/");
      const fromOwner = owner(local);
      if (!fromOwner) report(file, relative(file), "unknown-owner");
      if (local.startsWith("../") || path.isAbsolute(local)) {
        report(file, relative(file), "source-root-escape");
        continue;
      }
      if (fs.realpathSync(file) !== file) report(file, relative(file), "symlink-source");
      const source = project.program.getSourceFile(file);
      if (!source) throw new Error(`Missing parsed source: ${relative(file)}`);
      if (fromOwner === "generated" && !source.isDeclarationFile) report(file, relative(file), "generated-declarations-only");
      for (const reference of [...source.referencedFiles, ...source.typeReferenceDirectives]) {
        report(file, reference.fileName, "reference-forbidden");
      }
      function edge(specifier, typeOnly = false) {
        if (typeof specifier !== "string") {
          report(file, "<computed>", "computed-import");
          return;
        }
        if (!specifier.startsWith("./") && !specifier.startsWith("../")) {
          report(file, specifier, "relative-only");
          return;
        }
        if (!specifier.endsWith(".js") || specifier.includes("\\")) {
          report(file, specifier, "js-suffix");
          return;
        }
        let target = path.resolve(path.dirname(file), specifier.replace(/\.js$/, ".ts"));
        if (!fs.existsSync(target) && fs.existsSync(target.replace(/\.ts$/, ".d.ts"))) target = target.replace(/\.ts$/, ".d.ts");
        const targetLocal = path.relative(sourceRoot, target).split(path.sep).join("/");
        if (targetLocal.startsWith("../") || path.isAbsolute(targetLocal)) {
          report(file, relative(target), "source-root-escape");
          return;
        }
        const targetOwner = owner(targetLocal);
        if (!targetOwner) report(file, relative(target), "unknown-owner");
        if (!fs.existsSync(target) || !project.program.getSourceFile(target)) {
          report(file, relative(target), "unresolved-target");
        } else {
          pending.push(target);
          if (fs.realpathSync(target) !== target) report(file, relative(target), "symlink-source");
        }
        if (target.endsWith(".d.ts") && !typeOnly) report(file, relative(target), "declaration-type-only");
        const rule = boundaryRule(fromOwner, targetOwner, targetLocal, typeOnly);
        if (rule) report(file, relative(target), rule);
      }
      const literal = (node) => node && (ast.isStringLiteral(node) || ast.isNoSubstitutionTemplateLiteral(node)) ? node.text : undefined;
      const allTypeSpecifiers = (bindings) => bindings?.elements?.length > 0 && bindings.elements.every((item) => item.isTypeOnly);
      function visit(node) {
        if (ast.isImportDeclaration(node)) {
          const clause = node.importClause;
          const typeOnly = clause?.phaseModifier === ast.SyntaxKind.TypeKeyword || (!clause?.name && allTypeSpecifiers(clause?.namedBindings));
          edge(literal(node.moduleSpecifier), Boolean(typeOnly));
        } else if (ast.isExportDeclaration(node) && node.moduleSpecifier) {
          edge(literal(node.moduleSpecifier), node.isTypeOnly || Boolean(allTypeSpecifiers(node.exportClause)));
        } else if (ast.isImportTypeNode(node)) {
          edge(ast.isLiteralTypeNode(node.argument) ? literal(node.argument.literal) : undefined, true);
        } else if (ast.isImportEqualsDeclaration(node)) {
          report(file, literal(node.moduleReference.expression) || "<import-equals>", "commonjs-forbidden");
        } else if (ast.isCallExpression(node)) {
          if (node.expression.kind === ast.SyntaxKind.ImportKeyword) edge(literal(node.arguments[0]));
          if (ast.isIdentifier(node.expression) && node.expression.text === "require") report(file, literal(node.arguments[0]) || "<computed>", "commonjs-forbidden");
        } else if (ast.isIdentifier(node) && node.text === "require") {
          // Reject aliases too: const load = require; load(computed) is opaque.
          report(file, "<require>", "commonjs-forbidden");
        } else if (ast.isElementAccessExpression(node) && literal(node.argumentExpression) === "require") {
          report(file, "<require>", "commonjs-forbidden");
        } else if (ast.isModuleDeclaration(node) && ast.isStringLiteral(node.name)) {
          report(file, node.name.text, "ambient-module-forbidden");
        }
        node.forEachChild(visit);
      }
      visit(source);
    }
  } catch (error) {
    report(configPath, "<project>", "invalid-project", error.message);
  } finally {
    try {
      snapshot?.dispose();
    } finally {
      api.close();
    }
  }
  return [...diagnostics].sort();
}

function checkFrontendImports({ root }) {
  // The pinned native API inherits stderr and may print "context canceled"
  // while close() terminates its process. Contain that shutdown-only noise in
  // a disposable worker; preserve every other compiler failure as a diagnostic.
  return new Promise((resolve, reject) => {
    const child = fork(__filename, [], { stdio: ["ignore", "ignore", "pipe", "ipc"] });
    let diagnostics;
    let stderr = "";
    const timer = setTimeout(() => child.kill(), 25000);
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("message", (message) => { diagnostics = message; });
    child.on("error", reject);
    child.on("close", (code) => {
      clearTimeout(timer);
      const errors = stderr.split(/\r?\n/).filter((line) => line && line !== "context canceled");
      if (code !== 0 || !Array.isArray(diagnostics) || !diagnostics.every((item) => typeof item === "string")) {
        errors.push("Native snapshot worker did not complete");
      }
      resolve([...(diagnostics || []), ...errors.map((error) => `tsconfig.json -> <compiler> [native-error] ${error}`)].sort());
    });
    child.send({ root });
  });
}

module.exports = { checkFrontendImports };

if (require.main === module && process.send) {
  process.once("message", (options) => {
    inspectSnapshot(options).then((diagnostics) => {
      process.send(diagnostics, () => process.disconnect());
    }).catch((error) => {
      console.error(error.message);
      process.exitCode = 1;
      process.disconnect();
    });
  });
} else if (require.main === module) {
  const args = process.argv.slice(2);
  if (args.length && (args.length !== 2 || args[0] !== "--root")) {
    console.error("Usage: node scripts/check_frontend_imports.cjs [--root directory]");
    process.exitCode = 1;
  } else {
    checkFrontendImports({ root: args[1] || path.resolve(__dirname, "..") }).then((diagnostics) => {
      if (diagnostics.length) {
        console.error(diagnostics.join("\n"));
        process.exitCode = 1;
      } else {
        console.log(`Frontend import contracts passed (TypeScript ${version})`);
      }
    }).catch((error) => {
      console.error(`tsconfig.json -> <project> [invalid-project] ${error.message}`);
      process.exitCode = 1;
    });
  }
}
