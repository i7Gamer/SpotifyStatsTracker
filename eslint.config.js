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
    files: ["static/js/**/*.js", "tests/test_*.js"],
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
        // tests/test_*.js run under plain node, not a browser (see
        // tests/test_js_test_suite.py), and stub browser APIs onto `global`.
        ...globals.node,
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
];
