// In-page field extractor for the desktop apply-assist runner (T-026).
//
// Evaluated via Playwright's page.evaluate()/frame.evaluate() (see extract.py, which loads this
// file's source and wraps it as `() => { <this source>; return extractFields(); }`). Walks the
// DOM of whatever frame it runs in and returns a JSON-serializable array of
// `{field_id, label, type, required, options, current_value, section, selector}` for every
// fillable `<input>` (excluding hidden/submit/button types), `<select>` and `<textarea>`.
//
// field_id here is the *raw* per-frame id (element id, else name, else `field_<n>`); extract.py
// namespaces it per frame before combining frames' lists, so this function never needs to know
// which frame it is running in.
function extractFields() {
  function textOf(node) {
    return (node && node.textContent ? node.textContent : "").replace(/\s+/g, " ").trim();
  }

  function cssEscape(value) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(value);
    return String(value).replace(/([ #.;?%&,+*~':"!^$\[\]()=>|/@])/g, "\\$1");
  }

  // Priority: <label for> matching id > nearest ancestor <label> > aria-label > aria-labelledby
  // (joining referenced elements' text) > nearby text (preceding sibling text/element, or a
  // <fieldset>'s <legend>) > "".
  function resolveLabel(el) {
    const id = el.getAttribute("id");
    if (id) {
      const forLabel = document.querySelector('label[for="' + cssEscape(id) + '"]');
      if (forLabel) {
        const text = textOf(forLabel);
        if (text) return text;
      }
    }
    const wrapping = el.closest("label");
    if (wrapping) {
      const text = textOf(wrapping);
      if (text) return text;
    }
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const texts = labelledBy
        .split(/\s+/)
        .filter(Boolean)
        .map(function (refId) {
          const ref = document.getElementById(refId);
          return ref ? textOf(ref) : "";
        })
        .filter(Boolean);
      if (texts.length) return texts.join(" ");
    }
    let sib = el.previousSibling;
    while (sib) {
      if (sib.nodeType === 3 && sib.textContent && sib.textContent.trim()) {
        return sib.textContent.replace(/\s+/g, " ").trim();
      }
      if (sib.nodeType === 1 && sib.tagName !== "SCRIPT" && sib.tagName !== "STYLE") {
        const text = textOf(sib);
        if (text) return text;
      }
      sib = sib.previousSibling;
    }
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) {
        const text = textOf(legend);
        if (text) return text;
      }
    }
    return "";
  }

  // Best-effort grouping label: nearest ancestor <fieldset>'s <legend>, else the nearest
  // preceding <h1>-<h6> found by walking up the ancestor chain and scanning each level's earlier
  // siblings.
  function resolveSection(el) {
    let fieldset = el.closest("fieldset");
    while (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) {
        const text = textOf(legend);
        if (text) return text;
      }
      fieldset = fieldset.parentElement ? fieldset.parentElement.closest("fieldset") : null;
    }
    let node = el;
    while (node && node.tagName !== "BODY") {
      let sib = node.previousElementSibling;
      while (sib) {
        if (/^H[1-6]$/.test(sib.tagName)) {
          const text = textOf(sib);
          if (text) return text;
        }
        sib = sib.previousElementSibling;
      }
      node = node.parentElement;
    }
    return "";
  }

  // A CSS selector good enough to re-locate this element later (T-028's fill step): prefer #id
  // (own or nearest ancestor's), else a tag:nth-of-type(...) path from the nearest id'd ancestor
  // (or the document root) down to the element.
  function cssPath(el) {
    if (el.id) return "#" + cssEscape(el.id);
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && node.tagName !== "HTML") {
      if (node.id) {
        parts.unshift("#" + cssEscape(node.id));
        break;
      }
      let segment = node.tagName.toLowerCase();
      let index = 1;
      let sib = node;
      while ((sib = sib.previousElementSibling)) {
        if (sib.tagName === node.tagName) index++;
      }
      segment += ":nth-of-type(" + index + ")";
      parts.unshift(segment);
      node = node.parentElement;
    }
    return parts.join(" > ");
  }

  function fieldType(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === "select") return "select";
    if (tag === "textarea") return "textarea";
    return (el.getAttribute("type") || "text").toLowerCase();
  }

  function isRequired(el) {
    return el.hasAttribute("required") || el.getAttribute("aria-required") === "true";
  }

  function currentValue(el, type) {
    if (type === "file") return "";
    if (type === "checkbox" || type === "radio") return !!el.checked;
    return el.value != null ? el.value : "";
  }

  function selectOptions(el) {
    return Array.prototype.map.call(el.options, function (opt) {
      return { value: opt.value, text: textOf(opt) || opt.value };
    });
  }

  // Radio/checkbox groups sharing a `name`: list every member's {value, text}. A lone
  // checkbox/radio (no sibling sharing its name) is not a "group" — options stays [].
  function groupOptions(el, type) {
    const name = el.getAttribute("name");
    if (!name) return [];
    const group = Array.prototype.filter.call(document.querySelectorAll('input[type="' + type + '"]'), function (
      candidate
    ) {
      return candidate.getAttribute("name") === name;
    });
    if (group.length < 2) return [];
    return group.map(function (member) {
      return { value: member.value, text: resolveLabel(member) || member.value };
    });
  }

  const SKIP_TYPES = ["hidden", "submit", "button"];
  const candidates = Array.prototype.filter.call(document.querySelectorAll("input, select, textarea"), function (
    el
  ) {
    if (el.tagName.toLowerCase() !== "input") return true;
    const type = (el.getAttribute("type") || "text").toLowerCase();
    return SKIP_TYPES.indexOf(type) === -1;
  });

  const fields = [];
  candidates.forEach(function (el, index) {
    const type = fieldType(el);
    const id = el.getAttribute("id");
    const name = el.getAttribute("name");
    const fieldId = id || name || "field_" + index;
    let options = [];
    if (type === "select") {
      options = selectOptions(el);
    } else if (type === "radio" || type === "checkbox") {
      options = groupOptions(el, type);
    }
    fields.push({
      field_id: fieldId,
      label: resolveLabel(el),
      type: type,
      required: isRequired(el),
      options: options,
      current_value: currentValue(el, type),
      section: resolveSection(el),
      selector: cssPath(el),
    });
  });
  return fields;
}
