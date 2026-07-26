// Lint gate for the browser scripts in static/js, mirroring the philosophy
// already written down for ruff in pyproject.toml: `rules` below holds only
// what this codebase already passes, so going red MEANS something. It widens
// on purpose, each step landing with its violations fixed in the same commit.
//
// The gate exists because there was none. static/js/compare.js and wrapped.js
// each referenced `resp` inside a callback whose parameter is named `response`,
// which threw a ReferenceError on every load - taking out the whole Compare
// page and every Wrapped filter interaction - and shipped in a release.
// `no-undef` alone catches both, and nothing else in the toolchain could:
// these files only ever run in a browser, so the pytest suite never executes
// them.
//
// Deliberately NOT enabled, so nobody re-adds them expecting an easy win:
//   no-unused-vars - event-handler params (`e`, `event`) are conventionally
//                    kept for readability even when unread.
//   eqeqeq         - pre-existing `==`/`!=` comparisons throughout; a gate that
//                    fails on dozens of findings gets bypassed, then deleted.
// Stylistic/formatting rules are unused on purpose, same as ruff's formatter:
// reformatting every file would rewrite git blame across scripts whose comments
// carry the reasoning.
const globals = require("globals");

module.exports = [
  {
    // Browser scripts get BROWSER globals only. Spreading node's in here too
    // (they were in one block at first, for the test files below) quietly
    // excused `module`, `require`, `process` and `global` in static/js - real
    // copy-paste hazards in a tree where chart-utils.js is dual-use CJS, and
    // exactly the class of typo no-undef is here to catch.
    files: ["static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        ...globals.browser,
        // A real runtime global, not a missing declaration: charts.js ends with
        // `window.renderTimeSeriesChart = renderTimeSeriesChart`, and a window
        // property IS a global binding in a browser. wrapped.js calls it bare
        // (from an async callback, so charts.js has run by then even though
        // wrapped.html loads it second); detail-chart.js calls the same thing
        // window-qualified and guarded. no-undef can't tell the two styles
        // apart, so the name is declared here rather than the call sites
        // changed. Every other window.X export is only ever read qualified, or
        // from an inline on*= attribute in templates/.
        renderTimeSeriesChart: "readonly",
        // Only `module`, not node's whole set. Several of these scripts end with
        // `if (typeof module !== 'undefined' && module.exports) { … }` so their
        // pure logic can be required by the node unit tests - a deliberate
        // feature detection, not a stray global. Declaring the rest of node
        // would excuse `require`, `process`, `global` and `Buffer`, which in a
        // browser script are exactly the copy-paste slips no-undef exists for.
        module: "readonly",
      },
    },
    linterOptions: {
      // An unused disable comment is itself a finding: it means the code moved
      // on and the suppression outlived what it was suppressing.
      reportUnusedDisableDirectives: "error",
    },
    rules: {
      "no-undef": "error",            //< the ReferenceError class above
      "no-dupe-keys": "error",        //< a silently dropped object property
      "no-dupe-args": "error",
      "no-duplicate-case": "error",   //< an unreachable switch branch
      "no-unreachable": "error",
      "no-cond-assign": "error",      //< `if (a = b)` - almost always a typo'd ==
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-constant-condition": "error",
      "no-func-assign": "error",
      "no-obj-calls": "error",
      "no-sparse-arrays": "error",
      "no-unsafe-negation": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
    },
  },
  {
    // The JS unit tests run under plain node (see tests/test_js_test_suite.py)
    // and stub browser APIs onto `global`, so they need node's globals - and
    // browser's too, since what they exercise is browser code.
    files: ["tests/test_*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: { ...globals.browser, ...globals.node },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      "no-undef": "error",
      "no-dupe-keys": "error",
      "no-dupe-args": "error",
      "no-duplicate-case": "error",
      "no-unreachable": "error",
      "no-cond-assign": "error",
      "no-self-assign": "error",
      "no-self-compare": "error",
      "no-constant-condition": "error",
      "no-func-assign": "error",
      "no-obj-calls": "error",
      "no-sparse-arrays": "error",
      "no-unsafe-negation": "error",
      "use-isnan": "error",
      "valid-typeof": "error",
    },
  },
];
