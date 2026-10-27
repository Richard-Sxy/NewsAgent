"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const helpers = require("../app/modules/retrieval_lab/static/app.js");

test("source links reject executable schemes and embedded credentials", function () {
  ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>", "file:///etc/passwd", "https://user:secret@example.com/a", "//example.com/a", "not a URL"].forEach(function (url) {
    assert.equal(helpers.safeSourceUrl(url), null);
  });
  assert.equal(helpers.safeSourceUrl("https://www.news.cn/a?b=c#d"), "https://www.news.cn/a?b=c#d");
});

test("missing model metrics are never converted to zero accuracy", function () {
  [null, undefined, NaN, Infinity, "0.8"].forEach(function (value) { assert.equal(helpers.metricText(value, true), "待标注"); });
  assert.equal(helpers.metricText(0, true), "0.0%");
  assert.equal(helpers.metricText(0.75, true), "75.0%");
});

test("unable judgments are tracked separately from evaluated judgments", function () {
  const result = helpers.countsFrom([
    { judgment: { label: "relevant" } }, { judgment: { label: "partial" } },
    { judgment: { label: "irrelevant" } }, { judgment: { label: "unsure" } },
    { judgment: null }, { judgment: { label: "unexpected" } }
  ]);
  assert.deepEqual(result, { total: 6, judged: 3, unable: 1, touched: 4 });
});

test("a human selection and reviewer are mandatory before saving", function () {
  [null, undefined, "", "gold", "constructor", "__proto__"].forEach(function (label) {
    assert.throws(function () { helpers.buildJudgment({ revision: 0 }, label, "", "人工审核员"); }, /选择/);
  });
  assert.throws(function () { helpers.buildJudgment({ revision: 0 }, "relevant", "", "  "); }, /标注人/);
});

test("first save includes revision zero and preserves human notes", function () {
  assert.deepEqual(helpers.buildJudgment({ revision: 0 }, "partial", "  有背景但缺少进展  ", " 审核员 A "), {
    label: "partial", notes: "有背景但缺少进展", reviewer: "审核员 A", expected_revision: 0
  });
});

test("updates use persisted judgment revision for optimistic locking", function () {
  const result = helpers.buildJudgment({ revision: 1, judgment: { revision: 4 } }, "irrelevant", "", "审核员");
  assert.equal(result.expected_revision, 4);
});

test("corrupt revision cannot silently overwrite an existing judgment", function () {
  [-1, 1.5, "3", NaN].forEach(function (revision) {
    assert.throws(function () { helpers.buildJudgment({ judgment: { revision: revision } }, "relevant", "", "审核员"); }, /版本/);
  });
});

test("unable is sent as its own label rather than an irrelevant grade", function () {
  const result = helpers.buildJudgment({ revision: 0 }, "unsure", "正文不完整", "审核员");
  assert.equal(result.label, "unsure");
  assert.equal(Object.prototype.hasOwnProperty.call(result, "grade"), false);
});


test("an explicit unjudged object never counts as a saved human label", function () {
  assert.equal(helpers.hasJudgment({ judgment: null }), false);
  assert.equal(helpers.hasJudgment({ judgment: { label: null, status: "unjudged", revision: 0 } }), false);
  assert.equal(helpers.hasJudgment({ judgment: { label: "unsure", status: "unable", revision: 1 } }), true);
});
