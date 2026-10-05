/* Misa AOT runtime — DOM bridge shared state (Phase 4, M3).
 *
 * --pre-js file: defines the MisaDom element registry as a module-scope
 * global BEFORE the --js-library functions (rt/dom_bridge.js) that use
 * it. A separate --pre-js is required because Emscripten's `$`-prefixed
 * js-library dependency mechanism does not resolve when the calling
 * object file was not compiled by emcc itself (the Misa program object
 * comes from llvmlite): the `$MisaDom` symbol was emitted literally and
 * the state object was never defined (ReferenceError at boot).
 *
 * Element ids are small ints owned here; the C driver (rt/ui_dom.c)
 * only passes them back and forth.
 */
var MisaDom = {
  next: 0,
  els: {},
};
