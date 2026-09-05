const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const { createHash } = require("node:crypto");
const { spawnSync } = require("node:child_process");

const defaultRoot = path.resolve(__dirname, "..");

function isMaintained(relative) {
  return relative === "static/styles.css" || relative === "static/icons.svg" || relative === "static/favicon.svg" || relative === "static/favicon.ico" || relative === "static/apple-touch-icon.png" || relative === "static/fonts" || relative.startsWith("static/fonts/") || relative === "static/design/frame.css";
}

function isSource(relative) {
  return relative === "src" || relative === "html";
}

// This is a trusted-source include grammar, not an HTML template language.
function expandHtml(source, sourceRoot) {
  const root = fs.realpathSync(sourceRoot);
  if (root !== path.resolve(sourceRoot)) throw new Error("HTML source root cannot traverse a symbolic link");
  const cache = new Map();
  function render(reference, stack) {
    const match = /^([A-Za-z0-9][A-Za-z0-9._/-]*\.html)(?:#([A-Za-z][A-Za-z0-9_-]*))?$/.exec(reference);
    if (!match) throw new Error(`HTML invalid reference: ${reference}`);
    const [, filename, section] = match;
    const requested = path.resolve(root, filename);
    if (!requested.startsWith(`${root}${path.sep}`)) throw new Error("HTML reference escapes source root");
    let canonical;
    try { canonical = fs.realpathSync(requested); } catch { throw new Error(`HTML source missing: ${filename}`); }
    if (!canonical.startsWith(`${root}${path.sep}`)) throw new Error("HTML reference escapes source root");
    const identity = `${canonical}#${section ?? ""}`;
    if (stack.includes(identity)) throw new Error(`HTML include cycle: ${reference}`);
    if (!cache.has(canonical)) {
      const contents = fs.readFileSync(canonical, "utf8");
      const sections = new Map();
      const whole = [];
      let active = null;
      let cursor = 0;
      function append(part) {
        whole.push(part);
        if (active) active.parts.push(part);
      }
      // Recognize directives only at their original source positions. Joining
      // text around removed markers must never create a new include directive.
      while (cursor < contents.length) {
        const offset = contents.indexOf("<!--", cursor);
        if (offset === -1) {
          append(contents.slice(cursor));
          break;
        }
        append(contents.slice(cursor, offset));
        const end = contents.indexOf("-->", offset + 4);
        if (end === -1) throw new Error(`HTML unclosed comment in ${filename}`);
        const comment = contents.slice(offset, end + 3);
        if (comment.indexOf("<!--", 4) !== -1) throw new Error(`HTML nested comment in ${filename}`);
        cursor = end + 3;
        const body = comment.slice(4, -3).trim();
        if (!/^(include|fragment-start|fragment-end)\b/i.test(body)) {
          append(comment);
          continue;
        }
        const include = /^include ([A-Za-z0-9][A-Za-z0-9._/-]*\.html(?:#[A-Za-z][A-Za-z0-9_-]*)?)$/.exec(body);
        if (include) {
          append({ reference: include[1] });
          continue;
        }
        const marker = /^(fragment-start|fragment-end) ([A-Za-z][A-Za-z0-9_-]*)$/.exec(body);
        if (!marker) throw new Error(`HTML malformed marker in ${filename}`);
        const [, kind, name] = marker;
        if (kind === "fragment-start") {
          if (active || sections.has(name)) throw new Error(`HTML nested or duplicate section in ${filename}`);
          active = { name, parts: [] };
        } else {
          if (active?.name !== name) throw new Error(`HTML unmatched section in ${filename}`);
          sections.set(name, active.parts);
          active = null;
        }
      }
      if (active) throw new Error(`HTML unclosed marker in ${filename}`);
      cache.set(canonical, { whole, sections });
    }
    const parsed = cache.get(canonical);
    const selected = section ? parsed.sections.get(section) : parsed.whole;
    if (selected === undefined) throw new Error(`HTML section missing: ${reference}`);
    return selected.map((part) => typeof part === "string" ? part : render(part.reference, [...stack, identity])).join("");
  }
  return render(source, []).replace(/[\r\n]*$/, "\n");
}

// Never follow links when inspecting or publishing the served tree.
function filesIn(directory) {
  const files = new Map();
  function visit(current, prefix) {
    if (!fs.existsSync(current)) return;
    if (!fs.lstatSync(current).isDirectory()) throw new Error(`Expected directory: ${current}`);
    for (const name of fs.readdirSync(current).sort()) {
      const relative = prefix ? `${prefix}/${name}` : name;
      if (isSource(relative)) continue;
      const full = path.join(current, name);
      const stat = fs.lstatSync(full);
      if (stat.isDirectory()) visit(full, relative);
      else if (stat.isFile()) files.set(relative, fs.readFileSync(full));
      else throw new Error(`Unsupported artifact: ${relative}`);
    }
  }
  visit(directory, "");
  return new Map([...files].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
}

function assertOutputRoot(root, outputRoot) {
  const web = path.join(root, "web");
  if (outputRoot === root || root.startsWith(`${outputRoot}${path.sep}`) ||
      (outputRoot !== web && (outputRoot.startsWith(`${web}${path.sep}`) || web.startsWith(`${outputRoot}${path.sep}`)))) {
    throw new Error("Output root must be separate from source and repository roots");
  }
  // Check existing ancestors too: lexical containment alone permits symlink escape.
  for (let current = outputRoot; ; current = path.dirname(current)) {
    if (fs.existsSync(current) && fs.lstatSync(current).isSymbolicLink()) {
      throw new Error(`Output root cannot traverse a symbolic link: ${current}`);
    }
    if (path.dirname(current) === current) break;
  }
}

function publicationIdentity(outputRoot) {
  return createHash("sha256").update(outputRoot).digest("hex");
}

function publicationMarker(outputRoot) {
  return path.join(os.tmpdir(), `trellmark-frontend-${process.getuid()}-${publicationIdentity(outputRoot)}.publishing`);
}

function pruneEmptyGeneratedDirectories(directory, prefix = "") {
  if (!fs.existsSync(directory)) return;
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (!entry.isDirectory() || isMaintained(relative) || isSource(relative)) continue;
    const nested = path.join(directory, entry.name);
    pruneEmptyGeneratedDirectories(nested, relative);
    if (fs.readdirSync(nested).length === 0) fs.rmdirSync(nested);
  }
}

async function withOutputLock(outputRoot, operation, { wait = false } = {}) {
  // Linux abstract sockets are exclusive kernel-owned leases. Unlike PID files,
  // they disappear even after SIGKILL, without racing stale-lock reclamation.
  const address = `\0trellmark-frontend-${publicationIdentity(outputRoot)}`;
  const deadline = Date.now() + (wait ? 5000 : 0);
  let lease;
  while (!lease) {
    const candidate = net.createServer((socket) => socket.destroy());
    try {
      await new Promise((resolve, reject) => {
        candidate.once("error", reject);
        candidate.listen(address, resolve);
      });
      lease = candidate;
    } catch (error) {
      candidate.close();
      if (error.code !== "EADDRINUSE") throw error;
      if (Date.now() >= deadline) throw new Error("Frontend publication already in progress for this output root");
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }
  try {
    return await operation();
  } finally {
    await new Promise((resolve, reject) => lease.close((error) => error ? reject(error) : resolve()));
  }
}

async function buildFrontend({ root = defaultRoot, outputRoot } = {}) {
  root = fs.realpathSync(root);
  outputRoot = path.resolve(outputRoot ?? path.join(root, "web"));
  assertOutputRoot(root, outputRoot);
  return withOutputLock(outputRoot, () => generateFrontend(root, outputRoot));
}

function generateFrontend(root, outputRoot) {
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "trellmark-frontend-"));
  try {
    const result = spawnSync(process.execPath, [
      path.join(root, "node_modules/typescript/bin/tsc"),
      "--project", path.join(root, "tsconfig.json"),
      "--outDir", path.join(staging, "static"),
      "--strict", "--noEmitOnError", "--incremental", "false",
    ], { cwd: root, encoding: "utf8" });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`Frontend compilation failed\n${result.stdout}${result.stderr}`);
    if (filesIn(staging).size === 0) throw new Error("Frontend compilation emitted no files");
    const htmlRoot = path.join(root, "web/html");
    for (const page of ["index.html", "design.html"]) {
      if (fs.existsSync(path.join(htmlRoot, page))) {
        fs.writeFileSync(path.join(staging, page), expandHtml(page, htmlRoot));
      }
    }
    const expected = filesIn(staging);
    for (const relative of expected.keys()) {
      if (isMaintained(relative)) throw new Error(`Generated artifact overlaps maintained asset: ${relative}`);
    }
    const existing = filesIn(outputRoot);
    const marker = publicationMarker(outputRoot);
    // Kept outside served output; a subsequent complete build repairs a failed
    // publication. Remove only after every write, including stale-file cleanup.
    const markerFile = fs.openSync(marker, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_NOFOLLOW, 0o600);
    try {
      fs.writeFileSync(markerFile, "Publication incomplete; run a full frontend build.\n");
    } finally {
      fs.closeSync(markerFile);
    }
    for (const relative of existing.keys()) {
      if (!isMaintained(relative) && !expected.has(relative)) fs.unlinkSync(path.join(outputRoot, relative));
    }
    pruneEmptyGeneratedDirectories(outputRoot);
    for (const [relative, bytes] of expected) {
      if (existing.get(relative)?.equals(bytes)) continue;
      const destination = path.join(outputRoot, relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.writeFileSync(destination, bytes);
    }
    fs.unlinkSync(marker);
    return expected;
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

function parseArguments(args) {
  const options = {};
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === "--help") {
      console.log("Usage: node scripts/frontend_build.cjs [--root PATH] [--output-root PATH]\nGenerate TypeScript and trusted HTML includes into the web output root (static/ JavaScript plus index.html and optional design.html). Check without repairing: node scripts/check_frontend_artifacts.cjs (same options).\nMaintained assets: static/styles.css, static/icons.svg, static/favicon.svg, static/favicon.ico, static/apple-touch-icon.png, static/fonts/, static/design/frame.css. All other output files are generated or unknown and are checked by exact relative path and bytes. Top-level src/ and html/ remain source-only; includes resolve relative to web/html with optional named sections.\nPublishers use a Linux kernel lease; concurrent publishers fail. Staging and interruption markers stay outside served output. After an interrupted publication, run a full build to repair it; multi-file publication is not atomic on process kill.");
      return null;
    }
    if ((flag !== "--root" && flag !== "--output-root") || !args[index + 1] || args[index + 1].startsWith("--")) {
      throw new Error(`Unknown or incomplete argument: ${flag}`);
    }
    options[flag === "--root" ? "root" : "outputRoot"] = path.resolve(args[++index]);
  }
  return options;
}

if (require.main === module) {
  (async () => {
    const options = parseArguments(process.argv.slice(2));
    if (options) await buildFrontend(options);
  })().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

module.exports = { buildFrontend, expandHtml, filesIn, isMaintained, assertOutputRoot, parseArguments, withOutputLock, publicationMarker };
