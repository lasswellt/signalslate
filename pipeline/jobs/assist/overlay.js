// Shadow-DOM checklist overlay for the desktop apply-assist runner (T-029).
//
// Loaded the same way T-026's extract.js is loaded — as a sibling file, injected verbatim via
// Playwright's page.add_init_script()/page.evaluate() (see overlay.py, which reads this file's
// source and never inlines it as a Python string literal, so the two stay independently
// readable/editable). Runs top-level (it is not wrapped as a function-and-call the way extract.js
// is): the very act of loading this source installs the panel into the current document.
//
// Isolation: the panel lives entirely inside an `attachShadow({mode: "open"})` shadow root hung
// off a host <div> appended to document.body, so the job site's own CSS can never style/break the
// panel and the panel's own <style> can never leak onto the page. Every event listener this file
// adds is attached to an element inside that shadow root — the one deliberate exception is the
// narrow "outline a low-confidence page element" DOM touch below, which only ever sets a style
// attribute on element(s) named by a field's selector; it never attaches a listener to them, and
// nothing here ever calls document.addEventListener/window.addEventListener.
//
// Python bridge: four buttons call four Playwright-exposed functions (page.expose_function() in
// overlay.py, using these exact names) — window.__assistFillPage(), window.__assistNextStep(),
// window.__assistEditAnswer(fieldId, newValue), window.__assistPause(). Each exposed function
// returns a Promise (that's how Playwright's expose_function works from the page side); once it
// resolves, the handler records which action last completed on window.__assistLastAction so a
// test (or any other caller) can page.wait_for_function() on that instead of racing the async
// Python round-trip.
//
// Python -> panel: window.__assistRender(state) re-renders the whole panel from a state object
// shaped `{steps, fields, needs_user}` (see renderState() below for each key's shape). overlay.py
// calls this after every bridge callback does its work, and once immediately after install.
(function () {
  if (window.__assistOverlayInstalled) return;
  window.__assistOverlayInstalled = true;

  // Fields below this confidence get a visible outline, both in the panel's own list item and on
  // the actual page element (research doc "Apply Assist" > "Checklist overlay": "low-confidence
  // ones highlighted in the page"). 0.7 mirrors mapping.py's FieldProposal.confidence scale (0-1)
  // and is deliberately conservative: better to flag a borderline field for a human glance than to
  // let a shaky auto-fill slide by unmarked.
  var LOW_CONFIDENCE_THRESHOLD = 0.7;
  var HIGHLIGHT_ATTR = "data-assist-low-confidence";

  var _highlightedElements = [];
  var _lastFields = [];

  // --- Host + shadow root ----------------------------------------------------------------------

  var host = document.createElement("div");
  host.id = "__assist-overlay-host";
  // Resets any inherited CSS on the host itself; the shadow root is the real isolation boundary,
  // this just keeps the host <div> from picking up the page's own box model before that kicks in.
  host.style.all = "initial";
  host.style.position = "fixed";
  host.style.top = "0";
  host.style.right = "0";
  host.style.zIndex = "2147483647";
  document.body.appendChild(host);

  var shadow = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent = [
    ".assist-panel { all: initial; display: block; position: fixed; top: 12px; right: 12px;",
    "  width: 320px; max-height: 80vh; overflow-y: auto; background: #ffffff; color: #1a1a1a;",
    "  font-family: -apple-system, Segoe UI, Arial, sans-serif; font-size: 13px; line-height: 1.4;",
    "  border: 1px solid #c9c9c9; border-radius: 6px; box-shadow: 0 2px 10px rgba(0,0,0,0.25);",
    "  padding: 10px; box-sizing: border-box; }",
    ".assist-panel h2 { font-size: 14px; margin: 0 0 8px 0; }",
    ".assist-panel h3 { font-size: 12px; margin: 10px 0 4px 0; text-transform: uppercase; color: #555; }",
    ".assist-panel ol, .assist-panel ul { margin: 0; padding-left: 18px; }",
    ".assist-panel li { margin-bottom: 4px; word-break: break-word; }",
    ".assist-step--done { color: #2e7d32; }",
    ".assist-step--current { color: #1565c0; font-weight: bold; }",
    ".assist-step--pending { color: #888; }",
    ".assist-field--low-confidence { outline: 2px solid #e0a800; outline-offset: 2px; background: #fff8e1; }",
    ".assist-needs-user li { color: #b71c1c; }",
    ".assist-buttons { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }",
    ".assist-buttons button { flex: 1 1 auto; padding: 5px 6px; font-size: 12px; cursor: pointer; }",
  ].join("\n");
  shadow.appendChild(style);

  var panel = document.createElement("div");
  panel.className = "assist-panel";
  shadow.appendChild(panel);

  var titleEl = document.createElement("h2");
  titleEl.textContent = "Application Assist";
  panel.appendChild(titleEl);

  var stepsHeading = document.createElement("h3");
  stepsHeading.textContent = "Steps";
  panel.appendChild(stepsHeading);
  var stepsListEl = document.createElement("ol");
  panel.appendChild(stepsListEl);

  var fieldsHeading = document.createElement("h3");
  fieldsHeading.textContent = "Filled on this step";
  panel.appendChild(fieldsHeading);
  var fieldsListEl = document.createElement("ul");
  panel.appendChild(fieldsListEl);

  var needsUserHeading = document.createElement("h3");
  needsUserHeading.textContent = "Needs your attention";
  panel.appendChild(needsUserHeading);
  var needsUserListEl = document.createElement("ul");
  needsUserListEl.className = "assist-needs-user";
  panel.appendChild(needsUserListEl);

  var buttonsEl = document.createElement("div");
  buttonsEl.className = "assist-buttons";
  panel.appendChild(buttonsEl);

  function makeButton(label) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    buttonsEl.appendChild(btn);
    return btn;
  }

  var fillBtn = makeButton("Fill page");
  var nextBtn = makeButton("Next step");
  var editBtn = makeButton("Edit answer");
  var pauseBtn = makeButton("Pause");

  // --- Rendering ---------------------------------------------------------------------------------

  function renderSteps(steps) {
    stepsListEl.innerHTML = "";
    steps.forEach(function (step) {
      var status = step.status || "pending";
      var li = document.createElement("li");
      li.className = "assist-step assist-step--" + status;
      var marker = status === "done" ? "✓ " : status === "current" ? "▶ " : "· ";
      li.textContent = marker + (step.title || step.id || "");
      stepsListEl.appendChild(li);
    });
  }

  function renderFields(fields) {
    clearHighlights();
    _lastFields = fields;
    fieldsListEl.innerHTML = "";
    fields.forEach(function (field) {
      var confidence = typeof field.confidence === "number" ? field.confidence : 1;
      var isLow = confidence < LOW_CONFIDENCE_THRESHOLD;
      var li = document.createElement("li");
      li.className = "assist-field" + (isLow ? " assist-field--low-confidence" : "");
      var label = field.label || field.field_id || "";
      var value = field.value === undefined || field.value === null ? "" : String(field.value);
      var source = field.source || "?";
      li.textContent = label + ": " + value + " (source: " + source + ", confidence: " + confidence.toFixed(2) + ")";
      fieldsListEl.appendChild(li);
      if (isLow && field.selector) {
        highlightPageElement(field);
      }
    });
  }

  function renderNeedsUser(items) {
    needsUserListEl.innerHTML = "";
    items.forEach(function (item) {
      var li = document.createElement("li");
      var label = item.label || item.field_id || "";
      var reason = item.reason ? " - " + item.reason : "";
      li.textContent = label + reason;
      needsUserListEl.appendChild(li);
    });
  }

  function renderState(state) {
    state = state || {};
    renderSteps(state.steps || []);
    renderFields(state.fields || []);
    renderNeedsUser(state.needs_user || []);
  }

  // --- Live-page highlighting for low-confidence fields -------------------------------------------

  function clearHighlights() {
    _highlightedElements.forEach(function (el) {
      el.style.outline = "";
      el.style.outlineOffset = "";
      el.removeAttribute(HIGHLIGHT_ATTR);
    });
    _highlightedElements = [];
  }

  // Best-effort element lookup for a field's live page element, by the field's own `selector`
  // (pipeline.jobs.assist.extract's cssPath()) — never adds a listener to it, only ever a style
  // attribute (see module comment above). A "main:"-namespaced field_id (extract.py's scheme)
  // resolves against the top document directly. A "frame_<n>:"-namespaced field_id is a heuristic
  // only: extract.py's <n> is that frame's absolute index in page.frames (main frame usually at
  // index 0), so this guesses the (n-1)th <iframe> in document order and reaches into its
  // contentDocument. Any failure (wrong guess, cross-origin iframe, detached element) is caught and
  // silently skipped — a missed highlight is a cosmetic miss, not a correctness bug.
  function locatePageElement(field) {
    var selector = field.selector;
    if (!selector) return null;
    var fieldId = field.field_id || "";
    var prefix = fieldId.split(":")[0];
    if (prefix.indexOf("frame_") !== 0) {
      try {
        return document.querySelector(selector);
      } catch (e) {
        return null;
      }
    }
    try {
      var frameIndex = parseInt(prefix.slice("frame_".length), 10) - 1;
      var iframes = document.querySelectorAll("iframe");
      var iframeEl = iframes[frameIndex];
      if (!iframeEl || !iframeEl.contentDocument) return null;
      return iframeEl.contentDocument.querySelector(selector);
    } catch (e) {
      return null;
    }
  }

  function highlightPageElement(field) {
    var el = locatePageElement(field);
    if (!el) return;
    el.style.outline = "3px solid #e0a800";
    el.style.outlineOffset = "2px";
    el.setAttribute(HIGHLIGHT_ATTR, "true");
    _highlightedElements.push(el);
  }

  // --- Button -> Python bridge --------------------------------------------------------------------

  function callBridge(name, args, actionLabel) {
    var fn = window[name];
    if (typeof fn !== "function") return;
    Promise.resolve(fn.apply(null, args)).then(function () {
      window.__assistLastAction = actionLabel;
    });
  }

  fillBtn.addEventListener("click", function () {
    callBridge("__assistFillPage", [], "fill");
  });

  nextBtn.addEventListener("click", function () {
    callBridge("__assistNextStep", [], "next");
  });

  pauseBtn.addEventListener("click", function () {
    callBridge("__assistPause", [], "pause");
  });

  editBtn.addEventListener("click", function () {
    var defaultFieldId = _lastFields.length ? _lastFields[0].field_id : "";
    var fieldId = window.prompt("Field ID to edit:", defaultFieldId);
    if (fieldId === null) return;
    var newValue = window.prompt("New value:", "");
    if (newValue === null) return;
    callBridge("__assistEditAnswer", [fieldId, newValue], "edit");
  });

  // --- Public API exposed on window -----------------------------------------------------------

  window.__assistRender = renderState;
})();
