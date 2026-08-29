const { version } = require("typescript/package.json");

const expectedVersion = "7.0.2";
if (version !== expectedVersion) {
  console.error(`Expected TypeScript ${expectedVersion}, got ${version}`);
  process.exit(1);
}

console.log(`TypeScript package ${version} verified`);
