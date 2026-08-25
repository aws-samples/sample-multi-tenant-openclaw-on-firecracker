#!/usr/bin/env node
// OpenClaw 2026.7.1-2 does not fsync session transcript writes. Patch the
// installed, version-pinned bundle during image construction and fail closed if
// the upstream write paths no longer match the reviewed implementation.

import fs from "node:fs";
import path from "node:path";

const EXPECTED_VERSION = "2026.7.1-2";
const PATCH_MARKER = "openclaw-transcript-durability-issue-494-v1";

function fail(message) {
  console.error(`openclaw transcript durability patch: ${message}`);
  process.exit(1);
}

function replaceExactlyOnce(source, before, after, label) {
  const first = source.indexOf(before);
  const last = source.lastIndexOf(before);
  if (first < 0 || first !== last) {
    fail(`${label} expected exactly once, found ${first < 0 ? 0 : "multiple"}`);
  }
  return `${source.slice(0, first)}${after}${source.slice(first + before.length)}`;
}

function verifyPatchedSource(source) {
  const required = [
    `const OPENCLAW_TRANSCRIPT_DURABILITY_PATCH = "${PATCH_MARKER}";`,
    "syncTranscriptPathSync(filePath);",
    "await syncTranscriptPath(filePath);",
    "fsyncSync(fileFd);",
    "await fileHandle.sync();",
    "fsyncSync(parentFd);",
    "await parentHandle.sync();",
  ];
  for (const token of required) {
    if (!source.includes(token)) {
      fail(`patched bundle is missing ${JSON.stringify(token)}`);
    }
  }
  if ((source.match(/syncTranscriptPathSync\(filePath\);/g) ?? []).length !== 2) {
    fail("patched bundle must durably sync both synchronous transcript writers");
  }
  if ((source.match(/await syncTranscriptPath\(filePath\);/g) ?? []).length !== 3) {
    fail("patched bundle must durably sync all three asynchronous transcript writers");
  }
}

const packageRoot = process.argv[2] ? path.resolve(process.argv[2]) : "";
if (!packageRoot || process.argv.length !== 3) {
  fail("usage: patch-openclaw-transcript-durability.mjs <installed-openclaw-package-root>");
}

const packageJsonPath = path.join(packageRoot, "package.json");
let packageJson;
try {
  packageJson = JSON.parse(fs.readFileSync(packageJsonPath, "utf8"));
} catch (error) {
  fail(`cannot read ${packageJsonPath}: ${error.message}`);
}
if (packageJson.name !== "openclaw" || packageJson.version !== EXPECTED_VERSION) {
  fail(
    `refusing package ${packageJson.name ?? "<unknown>"}@${packageJson.version ?? "<unknown>"}; ` +
      `expected openclaw@${EXPECTED_VERSION}`,
  );
}

const distDir = path.join(packageRoot, "dist");
let candidates;
try {
  candidates = fs
    .readdirSync(distDir)
    .filter((name) => /^transcript-write-context-[A-Za-z0-9_-]+\.js$/.test(name));
} catch (error) {
  fail(`cannot inspect ${distDir}: ${error.message}`);
}
if (candidates.length !== 1) {
  fail(`expected one transcript-write-context bundle, found ${candidates.length}`);
}

const bundlePath = path.join(distDir, candidates[0]);
let source = fs.readFileSync(bundlePath, "utf8");
if (source.includes(PATCH_MARKER)) {
  verifyPatchedSource(source);
  console.log(`openclaw transcript durability patch: already applied to ${bundlePath}`);
  process.exit(0);
}

source = replaceExactlyOnce(
  source,
  'import { appendFileSync, writeFileSync } from "node:fs";',
  'import { appendFileSync, closeSync, fsyncSync, openSync, writeFileSync } from "node:fs";',
  "node:fs import",
);

source = replaceExactlyOnce(
  source,
  'import { AsyncLocalStorage } from "node:async_hooks";',
  `import { AsyncLocalStorage } from "node:async_hooks";
const OPENCLAW_TRANSCRIPT_DURABILITY_PATCH = "${PATCH_MARKER}";
function syncTranscriptPathSync(filePath) {
	const fileFd = openSync(filePath, "r+");
	try {
		fsyncSync(fileFd);
	} finally {
		closeSync(fileFd);
	}
	const parentFd = openSync(path.dirname(filePath), "r");
	try {
		fsyncSync(parentFd);
	} finally {
		closeSync(parentFd);
	}
}
async function syncTranscriptPath(filePath) {
	const fileHandle = await fs$1.open(filePath, "r+");
	try {
		await fileHandle.sync();
	} finally {
		await fileHandle.close();
	}
	const parentHandle = await fs$1.open(path.dirname(filePath), "r");
	try {
		await parentHandle.sync();
	} finally {
		await parentHandle.close();
	}
}`,
  "durability helper insertion point",
);

source = replaceExactlyOnce(
  source,
  '\twriteFileSync(filePath, content, "utf-8");',
  '\twriteFileSync(filePath, content, "utf-8");\n\tsyncTranscriptPathSync(filePath);',
  "synchronous transcript rewrite",
);
source = replaceExactlyOnce(
  source,
  '\tappendFileSync(filePath, content, "utf-8");',
  '\tappendFileSync(filePath, content, "utf-8");\n\tsyncTranscriptPathSync(filePath);',
  "synchronous transcript append",
);

source = replaceExactlyOnce(
  source,
  `	await fs$1.writeFile(filePath, serializeJsonlEntry(entry), {
		encoding: options?.encoding ?? "utf-8",
		...options?.flag ? { flag: options.flag } : {},
		...options?.mode !== void 0 ? { mode: options.mode } : {}
	});`,
  `	await fs$1.writeFile(filePath, serializeJsonlEntry(entry), {
		encoding: options?.encoding ?? "utf-8",
		...options?.flag ? { flag: options.flag } : {},
		...options?.mode !== void 0 ? { mode: options.mode } : {}
	});
	await syncTranscriptPath(filePath);`,
  "asynchronous single-entry transcript write",
);

source = replaceExactlyOnce(
  source,
  `	await fs$1.writeFile(filePath, content, {
		encoding: options?.encoding ?? "utf-8",
		...options?.flag ? { flag: options.flag } : {},
		...options?.mode !== void 0 ? { mode: options.mode } : {}
	});
	return content;`,
  `	await fs$1.writeFile(filePath, content, {
		encoding: options?.encoding ?? "utf-8",
		...options?.flag ? { flag: options.flag } : {},
		...options?.mode !== void 0 ? { mode: options.mode } : {}
	});
	await syncTranscriptPath(filePath);
	return content;`,
  "asynchronous transcript rewrite",
);

source = replaceExactlyOnce(
  source,
  `	} finally {
		await handle.close();
	}
}
//#endregion
//#region src/config/sessions/transcript-write-context.ts`,
  `	} finally {
		await handle.close();
	}
	await syncTranscriptPath(filePath);
}
//#endregion
//#region src/config/sessions/transcript-write-context.ts`,
  "asynchronous transcript append",
);

verifyPatchedSource(source);
const temporaryPath = `${bundlePath}.issue-494.tmp`;
fs.writeFileSync(temporaryPath, source, { encoding: "utf8", mode: 0o644, flag: "wx" });
fs.renameSync(temporaryPath, bundlePath);
console.log(`openclaw transcript durability patch: applied to ${bundlePath}`);
