// Image helper tests — no network/claude. Uses a real 1x1 PNG.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { encodeImages, saveImages, MAX_IMAGES } from "../src/images.js";

const PNG_1x1 = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

function tmp() { return mkdtempSync(join(tmpdir(), "cbimg-")); }

test("encodeImages reads a file and detects PNG by magic bytes", () => {
  const p = join(tmp(), "shot.png");
  writeFileSync(p, PNG_1x1);
  const [img] = encodeImages([p]);
  assert.equal(img.media_type, "image/png");
  assert.equal(img.name, "shot.png");
  assert.equal(Buffer.from(img.data, "base64").length, PNG_1x1.length);
});

test("encodeImages rejects a non-image", () => {
  const p = join(tmp(), "notes.txt");
  writeFileSync(p, "definitely not an image");
  assert.throws(() => encodeImages([p]), /not a supported image/);
});

test("encodeImages errors clearly on a missing file", () => {
  assert.throws(() => encodeImages(["/no/such/file.png"]), /cannot read image/);
});

test("encodeImages enforces the count cap", () => {
  assert.throws(() => encodeImages(Array(MAX_IMAGES + 1).fill("x.png")), /too many images/);
});

test("saveImages writes files that round-trip the bytes", () => {
  const images = [{ name: "shot.png", media_type: "image/png", data: PNG_1x1.toString("base64") }];
  const [path] = saveImages(images, join(tmp(), "saved"));
  assert.ok(existsSync(path));
  assert.deepEqual(readFileSync(path), PNG_1x1);
});
