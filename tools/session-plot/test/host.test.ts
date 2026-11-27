import { test } from "node:test";
import assert from "node:assert/strict";
import { isAllowedHost } from "../src/host";

test("accepts the three loopback forms with the right port", () => {
  assert.equal(isAllowedHost("127.0.0.1:4395", 4395), true);
  assert.equal(isAllowedHost("localhost:4395", 4395), true);
  assert.equal(isAllowedHost("[::1]:4395", 4395), true);
});

test("host matching is case-insensitive", () => {
  assert.equal(isAllowedHost("LOCALHOST:4395", 4395), true);
  assert.equal(isAllowedHost("Localhost:4395", 4395), true);
});

test("rejects the wrong port", () => {
  assert.equal(isAllowedHost("127.0.0.1:4396", 4395), false);
  assert.equal(isAllowedHost("localhost:80", 4395), false);
  assert.equal(isAllowedHost("[::1]:4396", 4395), false);
});

test("rejects other hosts, including a DNS-rebinding style name", () => {
  assert.equal(isAllowedHost("evil.example:4395", 4395), false);
  assert.equal(isAllowedHost("127.0.0.1.evil.example:4395", 4395), false);
  assert.equal(isAllowedHost("0.0.0.0:4395", 4395), false);
});

test("rejects a missing host header and a host without a port", () => {
  assert.equal(isAllowedHost(undefined, 4395), false);
  assert.equal(isAllowedHost("", 4395), false);
  assert.equal(isAllowedHost("127.0.0.1", 4395), false);
  assert.equal(isAllowedHost("[::1]", 4395), false);
});
