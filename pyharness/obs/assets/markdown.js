/* A small markdown renderer that builds DOM nodes.
 *
 * Everything it renders is untrusted: model prose, tool output, a page the
 * agent fetched, an eval board. So the one rule this file exists to keep is
 * that no string from the input ever reaches `innerHTML`. Text lands via
 * `textContent`, structure comes from `createElement`, and the only attribute
 * ever set from input is `href` — behind a scheme allowlist, because
 * `javascript:` in a link is the whole attack.
 *
 * Supported, because it is what actually shows up in these traces: fenced and
 * indented code, ATX headings, hr, blockquotes, nested lists, GFM pipe tables
 * (invoices and eval boards are full of them), and inline code / bold / italic
 * / strike / links / autolinks.
 *
 * Exposes `renderMarkdown(text) -> HTMLDivElement` and `looksLikeMarkdown(text)`.
 */

(function (global) {
  "use strict";

  var SAFE_SCHEME = /^(https?:|mailto:|#|\/)/i;

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  /* ---------------------------------------------------------------- inline */

  // Ordered by how greedily each pattern should win. Code first: a backtick
  // span is literal, so `**x**` inside it must not become bold.
  var INLINE = [
    { re: /`([^`\n]+)`/, node: function (m) { return el("code", null, m[1]); } },
    { re: /\*\*([^\n]+?)\*\*/, node: function (m) { return inlineInto(el("strong"), m[1]); } },
    { re: /__([^\n]+?)__/, node: function (m) { return inlineInto(el("strong"), m[1]); } },
    { re: /~~([^\n]+?)~~/, node: function (m) { return inlineInto(el("del"), m[1]); } },
    { re: /(?:^|(?<=[\s(]))\*([^*\n]+?)\*/, node: function (m) { return inlineInto(el("em"), m[1]); } },
    { re: /(?:^|(?<=[\s(]))_([^_\n]+?)_/, node: function (m) { return inlineInto(el("em"), m[1]); } },
    { re: /\[([^\]\n]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/, node: function (m) { return link(m[2], m[1] || m[2]); } },
    { re: /<((?:https?:\/\/|mailto:)[^>\s]+)>/, node: function (m) { return link(m[1], m[1]); } },
    { re: /(?:^|(?<=[\s(<]))(https?:\/\/[^\s<>()"']+[^\s<>()"'.,;:!?])/, node: function (m) { return link(m[1], m[1]); } },
  ];

  function link(href, text) {
    if (!SAFE_SCHEME.test(href)) return document.createTextNode(text);
    var a = el("a", null, text);
    a.href = href;
    if (/^https?:/i.test(href)) {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
    return a;
  }

  /** Append the inline rendering of `text` into `parent`, and return `parent`. */
  function inlineInto(parent, text) {
    var rest = String(text == null ? "" : text);
    while (rest) {
      var best = null;
      for (var i = 0; i < INLINE.length; i++) {
        var m = INLINE[i].re.exec(rest);
        if (m && (best === null || m.index < best.m.index)) {
          best = { m: m, rule: INLINE[i] };
          if (m.index === 0) break;
        }
      }
      if (!best) break;
      if (best.m.index > 0) parent.appendChild(document.createTextNode(rest.slice(0, best.m.index)));
      parent.appendChild(best.rule.node(best.m));
      rest = rest.slice(best.m.index + best.m[0].length);
    }
    if (rest) parent.appendChild(document.createTextNode(rest));
    return parent;
  }

  /* ----------------------------------------------------------------- table */

  function splitRow(line) {
    var cells = [];
    var cur = "";
    var esc = false;
    var body = line.trim().replace(/^\|/, "").replace(/\|$/, "");
    for (var i = 0; i < body.length; i++) {
      var c = body[i];
      if (esc) { cur += c; esc = false; continue; }
      if (c === "\\") { esc = true; continue; }
      if (c === "|") { cells.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  var DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

  function table(lines, start) {
    // A table is a header row, a `---|---` divider, then body rows. Anything
    // else and the caller falls through to paragraph handling.
    if (start + 1 >= lines.length) return null;
    if (lines[start].indexOf("|") === -1) return null;
    if (!DIVIDER.test(lines[start + 1])) return null;

    var head = splitRow(lines[start]);
    var i = start + 2;
    var rows = [];
    while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) {
      rows.push(splitRow(lines[i]));
      i++;
    }
    var wrap = el("div", "tablewrap");
    var t = el("table");
    var thead = el("thead");
    var tr = el("tr");
    head.forEach(function (h) { tr.appendChild(inlineInto(el("th"), h)); });
    thead.appendChild(tr);
    t.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (r) {
      var row = el("tr");
      for (var c = 0; c < head.length; c++) {
        row.appendChild(inlineInto(el("td"), r[c] === undefined ? "" : r[c]));
      }
      tbody.appendChild(row);
    });
    t.appendChild(tbody);
    wrap.appendChild(t);
    return { node: wrap, next: i };
  }

  /* ------------------------------------------------------------------ list */

  var BULLET = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;

  /** Consume one list (and its nested sublists) starting at `start`. */
  function list(lines, start) {
    var first = BULLET.exec(lines[start]);
    var indent = first[1].length;
    var ordered = /\d/.test(first[2]);
    var root = el(ordered ? "ol" : "ul");
    var i = start;
    var item = null;
    var buf = [];

    function flush() {
      if (!item) return;
      inlineInto(item, buf.join("\n").trim());
      buf = [];
    }

    while (i < lines.length) {
      var line = lines[i];
      var m = BULLET.exec(line);
      if (m && m[1].length <= indent) {
        if (m[1].length < indent) break;
        if (/\d/.test(m[2]) !== ordered) break;
        flush();
        item = el("li");
        root.appendChild(item);
        buf.push(m[3]);
        i++;
        continue;
      }
      if (m && m[1].length > indent && item) {
        flush();
        var sub = list(lines, i);
        item.appendChild(sub.node);
        i = sub.next;
        continue;
      }
      // A lazy continuation line: indented text under the current bullet.
      if (item && line.trim() && /^\s{2,}/.test(line)) { buf.push(line.trim()); i++; continue; }
      break;
    }
    flush();
    return { node: root, next: i };
  }

  /* ---------------------------------------------------------------- blocks */

  function render(text) {
    var out = el("div", "md");
    var lines = String(text == null ? "" : text).replace(/\r\n?/g, "\n").split("\n");
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];

      if (!line.trim()) { i++; continue; }

      // fenced code — ``` or ~~~, with an optional info string
      var fence = /^\s*(```+|~~~+)\s*([\w+-]*)\s*$/.exec(line);
      if (fence) {
        var close = fence[1][0];
        var body = [];
        i++;
        while (i < lines.length && !new RegExp("^\\s*" + close + "{" + fence[1].length + ",}\\s*$").test(lines[i])) {
          body.push(lines[i]);
          i++;
        }
        i++; // the closing fence (or the end of input)
        var pre = el("pre");
        var code = el("code", fence[2] ? "lang-" + fence[2] : null, body.join("\n"));
        pre.appendChild(code);
        out.appendChild(pre);
        continue;
      }

      var head = /^(#{1,6})\s+(.*?)\s*#*$/.exec(line);
      if (head) {
        out.appendChild(inlineInto(el("h" + Math.min(head[1].length, 4)), head[2]));
        i++;
        continue;
      }

      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.appendChild(el("hr")); i++; continue; }

      if (/^\s*>/.test(line)) {
        var quoted = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        var bq = el("blockquote");
        bq.appendChild(render(quoted.join("\n")));
        out.appendChild(bq);
        continue;
      }

      var tbl = table(lines, i);
      if (tbl) { out.appendChild(tbl.node); i = tbl.next; continue; }

      if (BULLET.test(line)) {
        var lst = list(lines, i);
        out.appendChild(lst.node);
        i = lst.next;
        continue;
      }

      // paragraph — runs to the next blank line or block opener
      var para = [];
      while (i < lines.length && lines[i].trim()) {
        var l = lines[i];
        if (/^\s*(```|~~~)/.test(l) || /^#{1,6}\s/.test(l) || /^\s*>/.test(l) ||
            BULLET.test(l) || (l.indexOf("|") !== -1 && DIVIDER.test(lines[i + 1] || ""))) break;
        para.push(l);
        i++;
      }
      if (para.length) {
        var p = el("p");
        for (var k = 0; k < para.length; k++) {
          if (k) p.appendChild(el("br"));
          inlineInto(p, para[k].trim());
        }
        out.appendChild(p);
      }
    }
    return out;
  }

  /** Is this text a *document*, or just program output that happens to contain
   *  a `#`?
   *
   *  This decides whether a tool result renders as prose or stays verbatim, so
   *  a loose answer is expensive in both directions: too eager and a tool's
   *  one-line `# browser — drive a headless browser…` blurb becomes a giant
   *  heading; too strict and a fetched invoice stays a wall of pipes. So: a
   *  table or a code fence is decisive on its own (nothing else produces
   *  either by accident), and anything weaker has to corroborate — two
   *  independent signals, where a lone heading is not one. */
  function looksLikeMarkdown(text) {
    var t = String(text == null ? "" : text);
    if (!t.trim()) return false;

    var lines = t.split("\n");
    for (var i = 0; i < lines.length - 1; i++) {
      if (lines[i].indexOf("|") !== -1 && DIVIDER.test(lines[i + 1])) return true;
    }
    if (/^\s*(```|~~~)/m.test(t)) return true;

    var count = function (re) { return (t.match(re) || []).length; };
    var signals =
      (count(/^#{1,6}\s+\S/gm) >= 2) +
      (count(/^\s*([-*+]|\d{1,9}[.)])\s+\S/gm) >= 2) +
      /\[[^\]\n]+\]\([^)\s]+\)/.test(t) +
      /\*\*[^\n*]+\*\*/.test(t);
    return signals >= 2;
  }

  global.renderMarkdown = render;
  global.looksLikeMarkdown = looksLikeMarkdown;
})(window);
