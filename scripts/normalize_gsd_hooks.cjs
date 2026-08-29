"use strict";

const fs = require("fs");
const path = require("path");

const hooksPath = ".codex/hooks.json";
const hooksDirectory = ".codex/hooks";
const configuration = JSON.parse(fs.readFileSync(hooksPath, "utf8"));
const hookFiles = fs.readdirSync(hooksDirectory).filter((file) =>
  [".js", ".cjs", ".mjs", ".sh"].includes(path.extname(file)),
);

let normalized = 0;
for (const groups of Object.values(configuration.hooks ?? {})) {
  for (const group of groups) {
    for (const handler of group.hooks ?? []) {
      if (handler.type !== "command") {
        continue;
      }

      const matches = hookFiles.filter((file) => handler.command.includes(file));
      if (matches.length !== 1) {
        throw new Error(`Cannot identify one local hook in command: ${handler.command}`);
      }

      const hookFile = matches[0];
      const extension = path.extname(hookFile);
      const runner = extension === ".sh" ? "bash" : "node";
      handler.command = `${runner} "$(git rev-parse --show-toplevel)/.codex/hooks/${hookFile}"`;
      normalized += 1;
    }
  }
}

if (normalized === 0) {
  throw new Error("The GSD installer did not register any command hooks.");
}

let duplicates = 0;
for (const [event, groups] of Object.entries(configuration.hooks)) {
  const seen = new Set();
  configuration.hooks[event] = groups.filter((group) => {
    const fingerprint = JSON.stringify(group);
    if (seen.has(fingerprint)) {
      duplicates += 1;
      return false;
    }
    seen.add(fingerprint);
    return true;
  });
}

fs.writeFileSync(hooksPath, `${JSON.stringify(configuration, null, 2)}\n`);
process.stdout.write(
  `Normalized ${normalized} project-local GSD hook commands and removed ${duplicates} exact duplicates.\n`,
);
