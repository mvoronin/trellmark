const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { buildFrontend, filesIn, isMaintained, assertOutputRoot, parseArguments, withOutputLock, publicationMarker } = require("./frontend_build.cjs");

async function checkFrontendArtifacts({ root = path.resolve(__dirname, ".."), outputRoot } = {}) {
  root = fs.realpathSync(root);
  outputRoot = path.resolve(outputRoot ?? path.join(root, "web"));
  assertOutputRoot(root, outputRoot);
  // Inspect the published tree before generating anything; never repair drift in check mode.
  const existing = await withOutputLock(outputRoot, () => {
    assertPublicationComplete(outputRoot);
    return filesIn(outputRoot);
  }, { wait: true });
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "trellmark-frontend-check-"));
  try {
    const expected = await buildFrontend({ root, outputRoot: path.join(temporary, "output") });
    return await withOutputLock(outputRoot, () => {
      assertPublicationComplete(outputRoot);
      const current = filesIn(outputRoot);
      if (current.size !== existing.size || [...existing].some(([relative, bytes]) => !current.get(relative)?.equals(bytes))) {
        throw new Error("Frontend output changed during artifact check; retry the check");
      }
      const diagnostics = [];
      const paths = [...new Set([...existing.keys(), ...expected.keys()])].sort();
      for (const relative of paths) {
        if (isMaintained(relative)) continue;
        if (!expected.has(relative)) diagnostics.push(`${relative}: unexpected artifact`);
        else if (!existing.has(relative)) diagnostics.push(`${relative}: missing artifact`);
        else if (!expected.get(relative).equals(existing.get(relative))) diagnostics.push(`${relative}: different bytes`);
      }
      return diagnostics;
    }, { wait: true });
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
}

function assertPublicationComplete(outputRoot) {
  if (fs.existsSync(publicationMarker(outputRoot))) {
    throw new Error("Frontend publication incomplete; run a full frontend build before checking artifacts");
  }
}

if (require.main === module) {
  (async () => {
    const options = parseArguments(process.argv.slice(2));
    if (options) {
      const diagnostics = await checkFrontendArtifacts(options);
      if (diagnostics.length) {
        console.error(diagnostics.join("\n"));
        process.exitCode = 1;
      } else console.log("Frontend artifacts match");
    }
  })().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

module.exports = { checkFrontendArtifacts };
