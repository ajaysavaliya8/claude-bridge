// http.js helper unit tests (netError, tokenOk, VERSION).

import { test } from "node:test";
import assert from "node:assert/strict";

import { netError, tokenOk, VERSION } from "../src/http.js";

test("VERSION is the package version", () => {
  assert.match(VERSION, /^\d+\.\d+\.\d+/);
});

test("netError maps common failure causes to readable text", () => {
  assert.match(netError({ cause: { code: "ECONNREFUSED" } }), /refused/i);
  assert.match(netError({ name: "AbortError" }), /timed out/i);
  assert.match(netError({ cause: { code: "ENOTFOUND" } }), /host not found/i);
  assert.match(netError({ cause: { code: "ECONNRESET" } }), /reset/i);
  assert.equal(netError({ message: "something else" }), "something else");
});

test("tokenOk: open with no token; constant-time Bearer match otherwise", () => {
  assert.equal(tokenOk({ headers: {} }, null), true); // no token configured → open
  assert.equal(tokenOk({ headers: {} }, "s3cret"), false); // missing header
  assert.equal(tokenOk({ headers: { authorization: "Bearer s3cret" } }, "s3cret"), true);
  assert.equal(tokenOk({ headers: { authorization: "Bearer wrong" } }, "s3cret"), false);
  assert.equal(tokenOk({ headers: { authorization: "s3cret" } }, "s3cret"), false); // no Bearer prefix
});
