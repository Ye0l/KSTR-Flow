import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

import { autocompletion, completionKeymap } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab, redo, undo } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { HighlightStyle, bracketMatching, indentOnInput, syntaxHighlighting } from "@codemirror/language";
import { setDiagnostics } from "@codemirror/lint";
import { searchKeymap } from "@codemirror/search";
import { EditorState, Prec } from "@codemirror/state";
import {
  Decoration,
  EditorView,
  ViewPlugin,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  hoverTooltip,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import { tags } from "@lezer/highlight";

const FLOW_CLASS = "KSTRFlow";
const STATE = Symbol("kstrFlowState");
const ROOT_STATE = Symbol("kstrFlowRootState");
const STATIC_INPUTS = new Set(["source", "global_seed"]);
const BUILTINS = [
  "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
  "int", "len", "list", "map", "max", "min", "range", "reversed",
  "round", "set", "sorted", "str", "sum", "tuple", "zip",
];
const RESERVED = ["global_seed", "seed", "random", "math", "nodes"];

let registryPromise = null;
const optionPromises = new Map();
let historyGuardInstalled = false;

function ensureHistoryGuard() {
  if (historyGuardInstalled) return;
  historyGuardInstalled = true;
  // Comfy's ChangeTracker listens during document capture and only exempts
  // INPUT/TEXTAREA. CodeMirror edits a contenteditable DIV, so Ctrl+Z would
  // otherwise restore the whole workflow before CodeMirror sees the key.
  window.addEventListener("keydown", (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.altKey) return;
    const key = event.key.toLowerCase();
    if (key !== "z" && key !== "y") return;
    const active = document.activeElement;
    const root = active?.closest?.(".kstr-flow-shell");
    const state = root?.[ROOT_STATE];
    if (!state?.view) return;

    if (key === "z" && !event.shiftKey) undo(state.view);
    else if ((key === "z" && event.shiftKey) || (key === "y" && !event.shiftKey)) redo(state.view);
    else return;
    // Even when CodeMirror has no matching history entry, never fall through to
    // Comfy's workflow-level undo while the editor owns keyboard focus.
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
  }, true);
}

function sourceWidget(node) {
  return node.widgets?.find((widget) => widget.name === "source");
}

function setWidgetHidden(widget, hidden) {
  if (!widget) return;
  widget.hidden = hidden;
  widget.options ??= {};
  widget.options.hidden = hidden;
  widget.computeSize = hidden ? () => [0, -4] : widget.computeSize;
}

function normalizeType(type) {
  if (!type || type === "ANY" || type === "typing.Any") return "*";
  if (!/^[A-Za-z_][A-Za-z0-9_*]*$/.test(type)) return "*";
  return type;
}

function findDynamicInput(node, name) {
  return node.inputs?.find((slot) => slot?.name === name);
}

function syncInputs(node, ports) {
  const wanted = new Set(ports.map((port) => port.name));
  for (let index = (node.inputs?.length ?? 0) - 1; index >= 0; index--) {
    const slot = node.inputs[index];
    if (slot && !STATIC_INPUTS.has(slot.name) && !wanted.has(slot.name)) node.removeInput(index);
  }

  for (const port of ports) {
    const type = normalizeType(port.type);
    let slot = findDynamicInput(node, port.name);
    if (!slot) {
      node.addInput(port.name, type);
      slot = node.inputs[node.inputs.length - 1];
      slot._kstrFlow = true;
    } else {
      slot.type = type;
      slot._kstrFlow = true;
    }
    slot.label = port.name;
  }
}

function syncOutputs(node, ports) {
  const outputs = node.outputs ?? [];
  for (let i = 0; i < ports.length; i++) {
    const name = ports[i].name || `output_${i}`;
    const type = normalizeType(ports[i].type);
    if (i < outputs.length) {
      outputs[i].name = name;
      outputs[i].label = name;
      outputs[i].type = type;
      outputs[i]._kstrFlow = true;
    } else {
      node.addOutput(name, type);
      node.outputs[node.outputs.length - 1]._kstrFlow = true;
    }
  }
  while ((node.outputs?.length ?? 0) > ports.length) node.removeOutput(node.outputs.length - 1);
}

async function loadInputOptions(pack, nodeName, inputName) {
  const key = `${pack}\0${nodeName}\0${inputName}`;
  if (!optionPromises.has(key)) {
    const params = new URLSearchParams({ pack, node: nodeName, input: inputName });
    optionPromises.set(key, api.fetchApi(`/kstr-flow/options?${params}`)
      .then((response) => {
        if (!response.ok) throw new Error(`options HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => Array.isArray(payload.options) ? payload.options : [])
      .catch((error) => { optionPromises.delete(key); throw error; }));
  }
  return optionPromises.get(key);
}

async function loadRegistry() {
  registryPromise ??= api.fetchApi("/kstr-flow/registry")
    .then((response) => {
      if (!response.ok) throw new Error(`registry HTTP ${response.status}`);
      return response.json();
    })
    .catch((error) => {
      registryPromise = null;
      throw error;
    });
  return registryPromise;
}

function signatureText(nodeInfo) {
  const args = (nodeInfo.inputs ?? []).map((input) => {
    const suffix = input.optional ? "?" : "";
    return `${input.name}: ${input.type}${suffix}`;
  }).join(", ");
  const outputList = nodeInfo.outputs ?? [];
  const outputs = outputList.length
    ? outputList.map((output) => output.name && output.name !== output.type ? `${output.name}: ${output.type}` : output.type).join(", ")
    : "None";
  return `(${args}) → ${outputList.length > 1 ? `(${outputs})` : outputs}`;
}

function makeNodeCompletion(nodeInfo) {
  const flags = [nodeInfo.deprecated && "deprecated", nodeInfo.experimental && "experimental"].filter(Boolean);
  const callName = nodeInfo.call_name || nodeInfo.name;
  const identity = nodeInfo.display_name && nodeInfo.display_name !== nodeInfo.name
    ? `${nodeInfo.display_name} · ${nodeInfo.name}`
    : nodeInfo.name;
  const searchLabel = [callName, nodeInfo.name, nodeInfo.display_name, ...(nodeInfo.search_aliases ?? [])]
    .filter(Boolean).join(" ");
  return {
    label: searchLabel,
    displayLabel: callName,
    sortText: callName,
    type: "function",
    detail: `${identity}  ${signatureText(nodeInfo)}`,
    info: [nodeInfo.description, flags.length ? `Flags: ${flags.join(", ")}` : null].filter(Boolean).join("\n\n"),
    apply: `${callName}(`,
  };
}

function findPackImport(text, pack) {
  for (const [alias, target] of importedAliases(text)) {
    if (target === pack) return { alias, imported: true };
  }
  return { alias: pack, imported: false };
}

function importInsertPoint(text) {
  let insertAt = 0;
  for (const match of text.matchAll(/^\s*import\s+[A-Za-z_]\w*(?:\s+as\s+[A-Za-z_]\w*)?\s*\n?/gm)) {
    insertAt = match.index + match[0].length;
  }
  return insertAt;
}

function applyNodeWithAutoImport(view, from, to, pack, nodeInfo) {
  let text = view.state.doc.toString();
  const imported = findPackImport(text, pack);
  let replaceFrom = from;
  let replaceTo = to;

  if (!imported.imported) {
    const insertAt = importInsertPoint(text);
    let importLine = `import ${pack}\n`;
    if (insertAt === 0 && text.length && !text.startsWith("\n")) importLine += "\n";
    view.dispatch({ changes: { from: insertAt, insert: importLine } });
    if (insertAt <= replaceFrom) {
      replaceFrom += importLine.length;
      replaceTo += importLine.length;
    }
    text = view.state.doc.toString();
  }

  const callName = nodeInfo.call_name || nodeInfo.name;
  const insertion = `${imported.alias}.${callName}(`;
  view.dispatch({
    changes: { from: replaceFrom, to: replaceTo, insert: insertion },
    selection: { anchor: replaceFrom + insertion.length },
    scrollIntoView: true,
  });
}

function makeGlobalNodeCompletion(pack, nodeInfo) {
  const callName = nodeInfo.call_name || nodeInfo.name;
  const aliases = (nodeInfo.search_aliases ?? []).join(", ");
  const searchLabel = [callName, nodeInfo.name, nodeInfo.display_name, ...(nodeInfo.search_aliases ?? [])]
    .filter(Boolean).join(" ");
  return {
    label: searchLabel,
    displayLabel: callName,
    sortText: callName,
    type: "function",
    detail: `${nodeInfo.display_name || nodeInfo.name} · ${pack} · ${nodeInfo.name}`,
    info: [signatureText(nodeInfo), nodeInfo.description, aliases ? `Aliases: ${aliases}` : null].filter(Boolean).join("\n\n"),
    apply(view, _completion, from, to) {
      applyNodeWithAutoImport(view, from, to, pack, nodeInfo);
    },
  };
}

function importedAliases(text) {
  const aliases = new Map();
  for (const match of text.matchAll(/^\s*import\s+([A-Za-z_]\w*)(?:\s+as\s+([A-Za-z_]\w*))?\s*$/gm)) {
    aliases.set(match[2] || match[1], match[1]);
  }
  return aliases;
}

function findCallAtCursor(textBefore, registry) {
  // Deliberately simple but useful: locate the innermost unfinished `pack.Node(`.
  const match = textBefore.match(/([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(([^()]*)$/s);
  if (!match) return null;
  const aliases = importedAliases(textBefore);
  const pack = aliases.get(match[1]) || match[1];
  const node = registry.packs?.[pack]?.find((item) =>
    (item.call_name || item.name) === match[2] || item.name === match[2]
  );
  return node ? { node, pack, fragment: match[3] } : null;
}

async function kstrCompletion(context) {
  let registry;
  try {
    registry = await loadRegistry();
  } catch {
    return null;
  }

  const doc = context.state.doc.toString();
  const before = doc.slice(0, context.pos);

  const importMatch = context.matchBefore(/\bimport\s+[A-Za-z_]*$/);
  if (importMatch) {
    const word = context.matchBefore(/[A-Za-z_]*$/);
    return {
      from: word.from,
      options: Object.keys(registry.packs ?? {}).sort().map((name) => ({
        label: name,
        type: "namespace",
        detail: `${registry.packs[name].length} nodes`,
      })),
    };
  }

  const memberMatch = context.matchBefore(/[A-Za-z_]\w*\.[A-Za-z_]*$/);
  if (memberMatch) {
    const [alias] = memberMatch.text.split(".");
    const pack = importedAliases(doc).get(alias) || alias;
    const nodes = registry.packs?.[pack];
    if (nodes) {
      return {
        from: memberMatch.from + alias.length + 1,
        options: nodes.map(makeNodeCompletion),
      };
    }
  }

  const call = findCallAtCursor(before, registry);
  if (call) {
    // If the cursor is inside a keyword value, offer the node's actual COMBO
    // values. Large lists (checkpoints/LoRAs/etc.) are fetched lazily.
    const valueMatch = call.fragment.match(/(?:^|,)\s*([A-Za-z_]\w*)\s*=\s*(["']?)([^,"']*)$/s);
    if (valueMatch) {
      const inputName = valueMatch[1];
      const quote = valueMatch[2];
      const typed = valueMatch[3] || "";
      const input = call.node.inputs.find((item) => item.name === inputName);
      if (input && (input.options?.length || input.option_count)) {
        let values = input.options ?? [];
        if (!values.length && input.option_count) {
          try { values = await loadInputOptions(call.pack, call.node.call_name || call.node.name, inputName); } catch { values = []; }
        }
        const from = context.pos - typed.length;
        const escapeQuoted = (value) => String(value).replaceAll("\\", "\\\\").replaceAll(quote || '"', `\\${quote || '"'}`);
        return {
          from,
          options: values.map((value) => {
            const isString = typeof value === "string";
            let apply;
            if (quote && isString) apply = `${escapeQuoted(value)}${quote}`;
            else apply = JSON.stringify(value);
            return {
              label: String(value),
              displayLabel: String(value),
              type: isString ? "text" : "constant",
              detail: `${call.node.display_name || call.node.name}.${inputName}`,
              apply,
            };
          }),
          validFor: /^[^,)]*$/,
        };
      }
    }

    const used = new Set([...call.fragment.matchAll(/\b([A-Za-z_]\w*)\s*=/g)].map((m) => m[1]));
    const word = context.matchBefore(/[A-Za-z_]*$/);
    const options = call.node.inputs
      .filter((input) => !used.has(input.name))
      .map((input) => ({
        label: `${input.name}=`,
        type: "property",
        detail: `${input.type}${input.optional ? " optional" : ""}${input.has_default ? ` = ${JSON.stringify(input.default)}` : ""}`,
        info: [
          input.tooltip,
          input.option_count ? `${input.option_count} available values` : null,
          input.options?.length ? input.options.slice(0, 24).join("\n") : null,
        ].filter(Boolean).join("\n\n"),
        apply: `${input.name}=`,
      }));
    if (options.length) return { from: word.from, options };
  }

  const word = context.matchBefore(/[A-Za-z_]\w*$/);
  if (!word || (!context.explicit && word.from === word.to)) return null;

  registry._kstrGlobalCompletions ??= Object.entries(registry.packs ?? {}).flatMap(([pack, nodes]) =>
    nodes.map((nodeInfo) => makeGlobalNodeCompletion(pack, nodeInfo))
  );
  const globalNodes = registry._kstrGlobalCompletions;

  return {
    from: word.from,
    options: [
      ...RESERVED.map((label) => ({ label, type: "variable", detail: "KSTR Flow runtime", boost: 50 })),
      ...BUILTINS.map((label) => ({ label, type: "function", detail: "safe builtin", boost: 40 })),
      ...globalNodes,
    ],
  };
}


const kstrHighlightStyle = HighlightStyle.define([
  { tag: tags.keyword, color: "#c792ea", fontWeight: "600" },
  { tag: [tags.string, tags.special(tags.string)], color: "#c3e88d" },
  { tag: [tags.number, tags.bool, tags.null], color: "#f78c6c" },
  { tag: tags.comment, color: "#6f7d8c", fontStyle: "italic" },
  { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: "#82aaff" },
  { tag: tags.typeName, color: "#ffcb6b" },
  { tag: tags.propertyName, color: "#89ddff" },
  { tag: [tags.operator, tags.punctuation], color: "#89a4b8" },
  { tag: tags.variableName, color: "#e8e8e8" },
]);

function semanticDecorations(view) {
  const text = view.state.doc.toString();
  const ranges = [];
  const seen = new Set();
  const add = (from, to, className) => {
    const key = `${from}:${to}:${className}`;
    if (from >= to || seen.has(key)) return;
    seen.add(key);
    ranges.push(Decoration.mark({ class: className }).range(from, to));
  };

  for (const match of text.matchAll(/\b(global_seed|seed|random|math|nodes)\b/g)) {
    add(match.index, match.index + match[0].length, "kstr-sem-reserved");
  }
  for (const match of text.matchAll(/\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*(?=\()/g)) {
    add(match.index, match.index + match[1].length, "kstr-sem-namespace");
    const memberStart = match.index + match[0].indexOf(match[2]);
    add(memberStart, memberStart + match[2].length, "kstr-sem-node");
  }
  for (const match of text.matchAll(/\b(?:IMAGE|MODEL|CLIP|VAE|LATENT|CONDITIONING|MASK|SEGS|STRING|INT|FLOAT|BOOLEAN|BOOL|COMBO)\b/g)) {
    add(match.index, match.index + match[0].length, "kstr-sem-type");
  }

  ranges.sort((a, b) => a.from - b.from || a.to - b.to);
  return Decoration.set(ranges, true);
}

const semanticPlugin = ViewPlugin.fromClass(class {
  constructor(view) { this.decorations = semanticDecorations(view); }
  update(update) {
    if (update.docChanged || update.viewportChanged) this.decorations = semanticDecorations(update.view);
  }
}, { decorations: (value) => value.decorations });

function wordRangeAt(doc, pos) {
  const text = doc.toString();
  let from = Math.max(0, Math.min(pos, text.length));
  let to = from;
  while (from > 0 && /[A-Za-z0-9_]/.test(text[from - 1])) from--;
  while (to < text.length && /[A-Za-z0-9_]/.test(text[to])) to++;
  if (from === to) return null;
  return { from, to, word: text.slice(from, to) };
}

function findNodeInfo(registry, pack, callName) {
  return registry.packs?.[pack]?.find((item) =>
    item.name === callName || (item.call_name || item.name) === callName
  ) ?? null;
}

function makeHoverDom(title, lines = [], description = "") {
  const root = document.createElement("div");
  root.className = "kstr-flow-hover";
  Object.assign(root.style, {
    maxWidth: "520px",
    padding: "8px 10px",
    color: "var(--fg-color, #e8e8e8)",
    background: "var(--comfy-menu-bg, #202124)",
    font: "11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    lineHeight: "1.45",
  });
  const heading = document.createElement("div");
  heading.textContent = title;
  Object.assign(heading.style, { fontWeight: "700", color: "#82aaff", marginBottom: "5px" });
  root.append(heading);
  for (const line of lines) {
    const row = document.createElement("div");
    row.textContent = line;
    root.append(row);
  }
  if (description) {
    const desc = document.createElement("div");
    desc.textContent = description;
    Object.assign(desc.style, { marginTop: "7px", opacity: ".72", whiteSpace: "pre-wrap", fontFamily: "ui-sans-serif, system-ui, sans-serif" });
    root.append(desc);
  }
  return root;
}

function nodeHoverLines(nodeInfo) {
  const inputs = (nodeInfo.inputs ?? []).map((input) => {
    const flags = [input.optional ? "optional" : null, input.has_default ? `default=${JSON.stringify(input.default)}` : null].filter(Boolean);
    return `  ${input.name}: ${input.type}${flags.length ? `  [${flags.join(", ")}]` : ""}`;
  });
  const outputs = (nodeInfo.outputs ?? []).map((output, index) =>
    `  ${output.name || `output_${index}`}: ${output.type}`
  );
  return ["inputs:", ...(inputs.length ? inputs : ["  (none)"]), "returns:", ...(outputs.length ? outputs : ["  None"])];
}

async function kstrHover(state, view, pos) {
  const range = wordRangeAt(view.state.doc, pos);
  if (!range) return null;
  let registry = state.registry;
  if (!registry) {
    try { registry = state.registry = await loadRegistry(); } catch { return null; }
  }
  const text = view.state.doc.toString();
  const before = text.slice(0, range.from);
  const member = before.match(/([A-Za-z_]\w*)\.\s*$/);
  if (member) {
    const alias = member[1];
    const pack = importedAliases(text).get(alias) || alias;
    const nodeInfo = findNodeInfo(registry, pack, range.word);
    if (nodeInfo) {
      return {
        pos: range.from,
        end: range.to,
        above: true,
        create: () => ({ dom: makeHoverDom(`${pack}.${nodeInfo.call_name || nodeInfo.name}`, nodeHoverLines(nodeInfo), nodeInfo.description || nodeInfo.display_name || "") }),
      };
    }
  }

  const call = findCallAtCursor(text.slice(0, range.from), registry);
  if (call) {
    const input = call.node.inputs?.find((item) => item.name === range.word);
    if (input) {
      const lines = [
        `type: ${input.type}`,
        input.optional ? "optional: yes" : "optional: no",
        input.has_default ? `default: ${JSON.stringify(input.default)}` : null,
        input.option_count ? `values: ${input.option_count} available` : null,
      ].filter(Boolean);
      return {
        pos: range.from,
        end: range.to,
        above: true,
        create: () => ({ dom: makeHoverDom(`${call.node.call_name || call.node.name}.${input.name}`, lines, input.tooltip || "") }),
      };
    }
  }

  const symbol = state.analysis?.symbols?.[range.word];
  if (symbol) {
    return {
      pos: range.from,
      end: range.to,
      above: true,
      create: () => ({ dom: makeHoverDom(range.word, [`type: ${symbol.type}`, `kind: ${symbol.kind || "variable"}`]) }),
    };
  }

  if (RESERVED.includes(range.word)) {
    return {
      pos: range.from,
      end: range.to,
      above: true,
      create: () => ({ dom: makeHoverDom(range.word, ["KSTR Flow runtime reserved value"]) }),
    };
  }

  const after = text.slice(range.to);
  if (/^\s*\(/.test(after)) {
    const matches = [];
    for (const [pack, nodes] of Object.entries(registry.packs ?? {})) {
      for (const info of nodes) if ((info.call_name || info.name) === range.word || info.name === range.word) matches.push({ pack, info });
    }
    if (matches.length === 1) {
      const { pack, info } = matches[0];
      return {
        pos: range.from,
        end: range.to,
        above: true,
        create: () => ({ dom: makeHoverDom(`${pack}.${info.call_name || info.name}`, nodeHoverLines(info), info.description || "") }),
      };
    }
  }
  return null;
}

const editorTheme = EditorView.theme({
  "&": {
    position: "absolute",
    inset: "0",
    height: "auto",
    minHeight: "0",
    minWidth: "0",
    fontSize: "12px",
    background: "var(--comfy-input-bg, #18181b)",
    color: "var(--fg-color, #ddd)",
  },
  ".cm-content": { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", padding: "8px 0" },
  ".cm-scroller": { overflow: "auto", minHeight: "0" },
  ".cm-gutters": { background: "transparent", border: "none", color: "#777" },
  ".cm-activeLine, .cm-activeLineGutter": { background: "rgba(127,127,127,.09)" },
  ".kstr-sem-namespace": { color: "#89ddff", fontWeight: "600" },
  ".kstr-sem-node": { color: "#82aaff", fontWeight: "600" },
  ".kstr-sem-type": { color: "#ffcb6b", fontWeight: "600" },
  ".kstr-sem-reserved": { color: "#f78c6c", fontWeight: "600" },
  ".cm-tooltip": {
    zIndex: "10000",
    background: "var(--comfy-menu-bg, #202124)",
    color: "var(--fg-color, #e5e5e5)",
    border: "1px solid rgba(127,127,127,.35)",
    boxShadow: "0 6px 20px rgba(0,0,0,.38)",
  },
  ".cm-tooltip-autocomplete > ul": { background: "var(--comfy-menu-bg, #202124)" },
  ".cm-tooltip-autocomplete > ul > li": { color: "var(--fg-color, #e5e5e5)" },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    background: "rgba(110,140,220,.32)",
    color: "#fff",
  },
  ".cm-completionDetail": { color: "rgba(220,220,220,.58)" },
  ".cm-completionInfo": {
    background: "var(--comfy-menu-bg, #202124)",
    color: "var(--fg-color, #e5e5e5)",
    border: "1px solid rgba(127,127,127,.35)",
  },
  ".cm-diagnosticText": { color: "var(--fg-color, #e5e5e5)" },
}, { dark: true });

function errorDiagnostic(view, error) {
  if (!error) return [];
  const lineNo = Math.max(1, Number(error.line || 1));
  const line = view.state.doc.line(Math.min(lineNo, view.state.doc.lines));
  const col = Math.max(0, Number(error.column || 1) - 1);
  const from = Math.min(line.to, line.from + col);
  return [{ from, to: Math.min(line.to, from + 1), severity: "error", message: error.message || String(error) }];
}

function bindInternalWheel(element, getScroller, signal) {
  element.dataset.captureWheel = "true";
  element.addEventListener("wheel", (event) => {
    const scroller = getScroller();
    if (!scroller) return;
    // Consume the wheel before Vue Nodes 2.0 can forward it to the canvas.
    // We scroll explicitly so this also works in legacy DOM-widget mode.
    event.preventDefault();
    event.stopPropagation();
    event.stopImmediatePropagation();
    if (event.ctrlKey || event.metaKey) return;
    if (event.shiftKey && event.deltaX === 0) scroller.scrollLeft += event.deltaY;
    else {
      scroller.scrollLeft += event.deltaX;
      scroller.scrollTop += event.deltaY;
    }
  }, { capture: true, passive: false, signal });
}

function makeShell() {
  const root = document.createElement("div");
  root.className = "kstr-flow-shell";
  Object.assign(root.style, {
    display: "grid",
    gridTemplateRows: "minmax(90px, 2fr) 6px minmax(140px, 3fr)",
    height: "100%",
    minHeight: "0",
    width: "100%",
    minWidth: "0",
    boxSizing: "border-box",
    overflow: "hidden",
    border: "1px solid rgba(127,127,127,.25)",
    borderRadius: "6px",
    background: "rgba(20,20,22,.94)",
    color: "var(--fg-color, #ddd)",
    contain: "size layout paint",
  });

  const previewPane = document.createElement("div");
  Object.assign(previewPane.style, {
    display: "grid",
    gridTemplateRows: "24px minmax(0,1fr)",
    minHeight: "0",
    minWidth: "0",
    overflow: "hidden",
    color: "var(--fg-color, #ddd)",
  });
  const previewHeader = document.createElement("div");
  previewHeader.textContent = "Graph Preview";
  Object.assign(previewHeader.style, {
    display: "flex",
    alignItems: "center",
    padding: "0 7px",
    borderBottom: "1px solid rgba(127,127,127,.14)",
    background: "rgba(127,127,127,.05)",
    opacity: ".72",
    font: "10px ui-sans-serif,system-ui,sans-serif",
  });
  const preview = document.createElement("div");
  preview.className = "kstr-flow-preview";
  Object.assign(preview.style, {
    position: "relative",
    minHeight: "0",
    minWidth: "0",
    overflow: "auto",
    overscrollBehavior: "contain",
    padding: "8px",
    userSelect: "none",
    color: "var(--fg-color, #ddd)",
  });
  preview.innerHTML = `<div style="opacity:.6;font:12px ui-monospace,monospace">waiting for analysis…</div>`;
  previewPane.append(previewHeader, preview);

  const divider = document.createElement("div");
  Object.assign(divider.style, { background: "rgba(127,127,127,.18)" });

  const editorPane = document.createElement("div");
  Object.assign(editorPane.style, {
    position: "relative",
    display: "grid",
    gridTemplateRows: "30px minmax(0,1fr)",
    minHeight: "0",
    minWidth: "0",
    overflow: "hidden",
  });

  const toolbar = document.createElement("div");
  Object.assign(toolbar.style, {
    display: "flex",
    alignItems: "center",
    gap: "6px",
    padding: "3px 6px",
    borderBottom: "1px solid rgba(127,127,127,.16)",
    background: "rgba(127,127,127,.05)",
    color: "var(--fg-color, #ddd)",
    font: "11px ui-sans-serif,system-ui,sans-serif",
  });
  const nodesButton = document.createElement("button");
  nodesButton.type = "button";
  nodesButton.textContent = "Nodes";
  nodesButton.title = "Browse installed ComfyUI nodes (Ctrl/Cmd+Shift+N)";
  Object.assign(nodesButton.style, {
    border: "1px solid rgba(127,127,127,.28)",
    borderRadius: "4px",
    padding: "2px 8px",
    background: "rgba(127,127,127,.10)",
    color: "inherit",
    cursor: "pointer",
  });
  const hint = document.createElement("span");
  hint.textContent = "installed node browser";
  Object.assign(hint.style, { opacity: ".48" });
  toolbar.append(nodesButton, hint);

  const editorHost = document.createElement("div");
  Object.assign(editorHost.style, {
    position: "relative",
    minHeight: "0",
    minWidth: "0",
    overflow: "hidden",
    contain: "strict",
  });

  const browser = document.createElement("div");
  Object.assign(browser.style, {
    position: "absolute",
    inset: "30px 0 0 0",
    zIndex: "20",
    display: "none",
    gridTemplateRows: "36px minmax(0,1fr)",
    background: "var(--comfy-menu-bg, rgba(20,20,22,.99))",
    color: "var(--fg-color, #ddd)",
    borderTop: "1px solid rgba(127,127,127,.22)",
  });

  const browserHeader = document.createElement("div");
  Object.assign(browserHeader.style, { display: "flex", gap: "6px", padding: "5px 6px" });
  const search = document.createElement("input");
  search.type = "search";
  search.placeholder = "Search node name, display name, alias, category…";
  Object.assign(search.style, {
    flex: "1",
    minWidth: "0",
    border: "1px solid rgba(127,127,127,.28)",
    borderRadius: "4px",
    padding: "4px 7px",
    background: "rgba(0,0,0,.20)",
    color: "inherit",
    outline: "none",
    font: "11px ui-monospace,monospace",
  });
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "×";
  close.title = "Close node browser";
  Object.assign(close.style, {
    width: "28px",
    border: "1px solid rgba(127,127,127,.25)",
    borderRadius: "4px",
    background: "transparent",
    color: "inherit",
    cursor: "pointer",
    fontSize: "16px",
  });
  browserHeader.append(search, close);

  const browserBody = document.createElement("div");
  Object.assign(browserBody.style, { display: "grid", gridTemplateColumns: "150px minmax(0,1fr)", minHeight: "0" });
  const packs = document.createElement("div");
  Object.assign(packs.style, { overflow: "auto", borderRight: "1px solid rgba(127,127,127,.16)", padding: "4px" });
  const results = document.createElement("div");
  Object.assign(results.style, { overflow: "auto", padding: "5px 7px" });
  browserBody.append(packs, results);
  browser.append(browserHeader, browserBody);

  editorPane.append(toolbar, editorHost, browser);
  root.append(previewPane, divider, editorPane);
  return {
    root, preview, editorHost, nodesButton, browser, browserSearch: search, browserClose: close,
    browserPacks: packs, browserResults: results,
  };
}

function graphLayout(graph) {
  const nodes = graph?.nodes ?? [];
  const edges = graph?.edges ?? [];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const outgoing = new Map(nodes.map((node) => [node.id, []]));
  for (const edge of edges) {
    if (!byId.has(edge.from) || !byId.has(edge.to)) continue;
    indegree.set(edge.to, (indegree.get(edge.to) ?? 0) + 1);
    outgoing.get(edge.from)?.push(edge.to);
  }

  const layer = new Map(nodes.map((node) => [node.id, 0]));
  const queue = nodes.filter((node) => (indegree.get(node.id) ?? 0) === 0).map((node) => node.id);
  let cursor = 0;
  while (cursor < queue.length) {
    const id = queue[cursor++];
    for (const next of outgoing.get(id) ?? []) {
      layer.set(next, Math.max(layer.get(next) ?? 0, (layer.get(id) ?? 0) + 1));
      indegree.set(next, (indegree.get(next) ?? 1) - 1);
      if (indegree.get(next) === 0) queue.push(next);
    }
  }

  // Cyclic/unresolved nodes still need a deterministic place.
  const unresolved = nodes.filter((node) => !queue.includes(node.id));
  for (const node of unresolved) layer.set(node.id, layer.get(node.id) ?? 0);

  const groups = new Map();
  for (const node of nodes) {
    const value = layer.get(node.id) ?? 0;
    if (!groups.has(value)) groups.set(value, []);
    groups.get(value).push(node);
  }

  const positions = new Map();
  const nodeWidth = 154;
  const nodeHeight = 42;
  const xGap = 52;
  const yGap = 14;
  let maxLayer = 0;
  let maxRows = 1;
  for (const [layerIndex, group] of groups) {
    maxLayer = Math.max(maxLayer, layerIndex);
    maxRows = Math.max(maxRows, group.length);
    group.sort((a, b) => a.label.localeCompare(b.label));
    group.forEach((node, row) => {
      positions.set(node.id, {
        x: 16 + layerIndex * (nodeWidth + xGap),
        y: 24 + row * (nodeHeight + yGap),
        width: nodeWidth,
        height: nodeHeight,
      });
    });
  }

  return {
    positions,
    width: 32 + (maxLayer + 1) * nodeWidth + maxLayer * xGap,
    height: 48 + maxRows * nodeHeight + Math.max(0, maxRows - 1) * yGap,
  };
}

function svgEl(name, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}

function renderGraph(preview, graph) {
  preview.replaceChildren();
  if (!graph?.nodes?.length) {
    const empty = document.createElement("div");
    empty.textContent = "Graph preview · no Comfy nodes on the default path";
    Object.assign(empty.style, { opacity: ".6", font: "12px ui-monospace,monospace" });
    preview.append(empty);
    return;
  }

  const { positions, width, height } = graphLayout(graph);
  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
  Object.assign(svg.style, { display: "block", minWidth: `${width}px`, minHeight: `${height}px` });

  const edgesGroup = svgEl("g", { fill: "none", stroke: "currentColor", "stroke-opacity": ".28", "stroke-width": "1.25" });
  for (const edge of graph.edges ?? []) {
    const from = positions.get(edge.from);
    const to = positions.get(edge.to);
    if (!from || !to) continue;
    const x1 = from.x + from.width;
    const y1 = from.y + from.height / 2;
    const x2 = to.x;
    const y2 = to.y + to.height / 2;
    const bend = Math.max(24, (x2 - x1) * 0.48);
    const path = svgEl("path", { d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}` });
    const title = svgEl("title");
    title.textContent = edge.to_input ? `${edge.to_input}` : "connection";
    path.append(title);
    edgesGroup.append(path);
  }
  svg.append(edgesGroup);

  for (const node of graph.nodes) {
    const pos = positions.get(node.id);
    if (!pos) continue;
    const group = svgEl("g", { transform: `translate(${pos.x},${pos.y})` });
    const isBoundary = node.kind === "input" || node.kind === "output";
    const rect = svgEl("rect", {
      width: pos.width,
      height: pos.height,
      rx: 6,
      fill: isBoundary ? "rgba(127,127,127,.10)" : "rgba(127,127,127,.16)",
      stroke: "currentColor",
      "stroke-opacity": isBoundary ? ".28" : ".42",
    });
    group.append(rect);

    const title = svgEl("title");
    title.textContent = node.kind === "node"
      ? `${node.namespace}.${node.class_type}${node.category ? `\n${node.category}` : ""}`
      : `${node.kind}: ${node.label} (${node.type || "*"})`;
    rect.append(title);

    const label = svgEl("text", { x: 9, y: 17, fill: "currentColor", "font-size": "11", "font-family": "ui-monospace,monospace" });
    const shown = node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label;
    label.textContent = shown;
    group.append(label);

    const sub = svgEl("text", { x: 9, y: 32, fill: "currentColor", "fill-opacity": ".52", "font-size": "9.5", "font-family": "ui-monospace,monospace" });
    sub.textContent = node.kind === "node" ? node.namespace : `${node.kind.toUpperCase()} · ${node.type || "*"}`;
    group.append(sub);
    svg.append(group);
  }

  preview.append(svg);
}

function renderPreview(state) {
  const { preview, analysis } = state;
  preview.replaceChildren();
  if (state.error && (!analysis || analysis.ok === false)) {
    const error = document.createElement("div");
    error.textContent = state.error.message || "Analysis failed";
    Object.assign(error.style, { color: "#e57373", font: "12px ui-monospace,monospace", whiteSpace: "pre-wrap" });
    preview.append(error);
    return;
  }
  if (!analysis) {
    const waiting = document.createElement("div");
    waiting.textContent = "Graph preview · analyzing…";
    Object.assign(waiting.style, { opacity: ".6", font: "12px ui-monospace,monospace" });
    preview.append(waiting);
    return;
  }
  if (!analysis.ok) {
    const error = document.createElement("div");
    error.textContent = analysis.error?.message || "Syntax error";
    Object.assign(error.style, { color: "#e57373", font: "12px ui-monospace,monospace" });
    preview.append(error);
    return;
  }
  renderGraph(preview, analysis.graph);
  if (analysis.graph_error) {
    const warning = document.createElement("div");
    warning.textContent = `Preview: ${analysis.graph_error}`;
    Object.assign(warning.style, {
      position: "sticky",
      left: "0",
      bottom: "0",
      display: "inline-block",
      padding: "3px 6px",
      font: "10px ui-monospace,monospace",
      background: "rgba(25,25,28,.88)",
      opacity: ".75",
    });
    preview.append(warning);
  }
}

function browserSearchText(pack, info) {
  return [
    pack, info.name, info.call_name, info.display_name, info.category, info.description,
    ...(info.search_aliases ?? []),
  ].filter(Boolean).join(" ").toLowerCase();
}

function styleBrowserButton(button, active = false) {
  Object.assign(button.style, {
    display: "block",
    width: "100%",
    border: "0",
    borderRadius: "3px",
    padding: "4px 6px",
    textAlign: "left",
    background: active ? "rgba(127,127,127,.18)" : "transparent",
    color: "inherit",
    cursor: "pointer",
    font: "11px ui-sans-serif,system-ui,sans-serif",
  });
}

function ensurePackImport(view, pack) {
  const text = view.state.doc.toString();
  const existing = findPackImport(text, pack);
  if (existing.imported) return { alias: existing.alias, insertAt: -1, delta: 0 };

  const insertAt = importInsertPoint(text);
  let line = `import ${pack}\n`;
  if (insertAt === 0 && text.length && !text.startsWith("\n")) line += "\n";
  view.dispatch({ changes: { from: insertAt, insert: line } });
  return { alias: pack, insertAt, delta: line.length };
}

function insertNodeCall(state, pack, info) {
  const view = state.view;
  if (!view) return;
  const cursorBefore = view.state.selection.main.head;
  const imported = ensurePackImport(view, pack);
  const cursor = cursorBefore + (imported.insertAt >= 0 && imported.insertAt <= cursorBefore ? imported.delta : 0);
  const callName = info.call_name || info.name;
  const insertion = `${imported.alias}.${callName}()`;
  view.dispatch({
    changes: { from: cursor, to: cursor, insert: insertion },
    selection: { anchor: cursor + insertion.length - 1 },
    scrollIntoView: true,
  });
  view.focus();
}

function makeNodeRow(state, pack, info) {
  const row = document.createElement("button");
  row.type = "button";
  styleBrowserButton(row, false);
  Object.assign(row.style, { padding: "6px 7px", borderBottom: "1px solid rgba(127,127,127,.08)" });
  const top = document.createElement("div");
  top.textContent = info.display_name || info.name;
  Object.assign(top.style, { fontWeight: "600", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
  const sub = document.createElement("div");
  sub.textContent = `${pack}.${info.call_name || info.name}  ·  ${info.name}`;
  Object.assign(sub.style, { opacity: ".58", font: "10px ui-monospace,monospace", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
  row.append(top, sub);
  row.title = [info.description, signatureText(info)].filter(Boolean).join("\n\n");
  row.addEventListener("click", () => {
    insertNodeCall(state, pack, info);
    state.browserOpen = false;
    state.shell.browser.style.display = "none";
  });
  return row;
}

function renderNodeBrowser(state) {
  const registry = state.registry;
  if (!registry) return;
  const { browserPacks: packsHost, browserResults: resultsHost, browserSearch: search } = state.shell;
  const query = search.value.trim().toLowerCase();
  const packNames = Object.keys(registry.packs ?? {}).sort((a, b) => a.localeCompare(b));
  const selected = state.browserPack && registry.packs?.[state.browserPack] ? state.browserPack : null;

  packsHost.replaceChildren();
  const all = document.createElement("button");
  all.type = "button";
  all.textContent = `All (${packNames.reduce((n, pack) => n + registry.packs[pack].length, 0)})`;
  styleBrowserButton(all, !selected);
  all.addEventListener("click", () => { state.browserPack = null; renderNodeBrowser(state); });
  packsHost.append(all);
  for (const pack of packNames) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${pack} (${registry.packs[pack].length})`;
    button.title = pack;
    styleBrowserButton(button, selected === pack);
    button.addEventListener("click", () => { state.browserPack = pack; renderNodeBrowser(state); });
    packsHost.append(button);
  }

  resultsHost.replaceChildren();
  if (!query && !selected) {
    const help = document.createElement("div");
    help.textContent = "Select a pack on the left, or search across all installed nodes.";
    Object.assign(help.style, { opacity: ".58", padding: "10px", font: "11px ui-monospace,monospace" });
    resultsHost.append(help);
    return;
  }

  let matches = [];
  for (const pack of packNames) {
    if (selected && pack !== selected) continue;
    for (const info of registry.packs[pack]) {
      if (!query || browserSearchText(pack, info).includes(query)) matches.push({ pack, info });
    }
  }
  matches.sort((a, b) => {
    const cat = String(a.info.category || "Other").localeCompare(String(b.info.category || "Other"));
    return cat || String(a.info.display_name || a.info.name).localeCompare(String(b.info.display_name || b.info.name));
  });

  if (!matches.length) {
    const empty = document.createElement("div");
    empty.textContent = "No matching installed nodes";
    Object.assign(empty.style, { opacity: ".55", padding: "8px", font: "11px ui-monospace,monospace" });
    resultsHost.append(empty);
    return;
  }

  const grouped = new Map();
  for (const item of matches.slice(0, query ? 300 : 1200)) {
    const category = item.info.category || "Other";
    if (!grouped.has(category)) grouped.set(category, []);
    grouped.get(category).push(item);
  }
  for (const [category, items] of grouped) {
    const details = document.createElement("details");
    details.open = Boolean(query) || grouped.size <= 8;
    const summary = document.createElement("summary");
    summary.textContent = `${category} (${items.length})`;
    Object.assign(summary.style, { cursor: "pointer", padding: "5px 3px", opacity: ".82", font: "11px ui-monospace,monospace" });
    details.append(summary);
    for (const { pack, info } of items) details.append(makeNodeRow(state, pack, info));
    resultsHost.append(details);
  }
}

async function openNodeBrowser(state) {
  state.browserOpen = true;
  state.shell.browser.style.display = "grid";
  state.shell.browserSearch.focus();
  if (!state.registry) {
    state.shell.browserResults.textContent = "Loading installed nodes…";
    try {
      state.registry = await loadRegistry();
    } catch (error) {
      state.shell.browserResults.textContent = `Failed to load node registry: ${error}`;
      return;
    }
  }
  renderNodeBrowser(state);
}

function closeNodeBrowser(state) {
  state.browserOpen = false;
  state.shell.browser.style.display = "none";
  state.view?.focus();
}

async function analyzeState(node, state) {
  if (!state?.view || state.destroyed) return;
  const source = state.view.state.doc.toString();
  const generation = ++state.generation;
  state.analysis = null;
  state.error = null;
  renderPreview(state);

  const requestController = new AbortController();
  state.analyzeController?.abort();
  state.analyzeController = requestController;
  const timeout = setTimeout(() => requestController.abort("analysis timeout"), 10000);

  try {
    const response = await api.fetchApi("/kstr-flow/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
      signal: requestController.signal,
    });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`analyze HTTP ${response.status}${body ? `: ${body.slice(0, 240)}` : ""}`);
    }
    const result = await response.json();
    if (generation !== state.generation) return;
    state.analysis = result;
    state.error = result.ok ? null : result.error;
    if (result.ok) {
      syncInputs(node, result.inputs ?? []);
      syncOutputs(node, result.outputs ?? []);
    }
    setDiagnostics(state.view, errorDiagnostic(state.view, state.error));
    renderPreview(state);
    node.setDirtyCanvas?.(true, true);
  } catch (error) {
    if (generation !== state.generation) return;
    const message = requestController.signal.aborted
      ? "Analysis request timed out after 10s"
      : String(error);
    state.error = { message, line: 1, column: 1 };
    state.analysis = { ok: false, error: state.error, graph: null, graph_error: null, symbols: {} };
    setDiagnostics(state.view, errorDiagnostic(state.view, state.error));
    renderPreview(state);
    console.error("[KSTR Flow] analysis failed", error);
  } finally {
    clearTimeout(timeout);
    if (state.analyzeController === requestController) state.analyzeController = null;
  }
}

function scheduleAnalyze(node, state = node[STATE], delay = 180) {
  if (!state || state.destroyed) return;
  clearTimeout(state.timer);
  state.timer = setTimeout(() => {
    if (!state.destroyed) void analyzeState(node, state);
  }, delay);
}

function syncEditorFromWidget(node) {
  const state = node[STATE];
  if (!state?.view || !state.sourceWidget) return;
  const source = String(state.sourceWidget.value ?? "");
  const current = state.view.state.doc.toString();
  if (source === current) return;
  state.view.dispatch({ changes: { from: 0, to: current.length, insert: source } });
}

function install(node) {
  if (node[STATE]) return;

  // Undo/redo can restore the same LiteGraph node object after its DOM widget
  // was unmounted. Remove any stale editor widget before creating a fresh one.
  for (const widget of [...(node.widgets ?? [])]) {
    if (widget?.name === "kstr_flow_editor") {
      try { node.removeWidget(widget); } catch {}
    }
  }

  const source = sourceWidget(node);
  if (!source) return;
  setWidgetHidden(source, true);

  const shell = makeShell();
  const controller = new AbortController();
  const state = node[STATE] = {
    generation: 0,
    timer: null,
    analysis: null,
    error: null,
    sourceWidget: source,
    preview: shell.preview,
    shell,
    view: null,
    registry: null,
    browserOpen: false,
    browserPack: null,
    controller,
    destroyed: false,
  };
  shell.root[ROOT_STATE] = state;
  ensureHistoryGuard();

  const editorState = EditorState.create({
    doc: String(source.value ?? ""),
    extensions: [
      lineNumbers(),
      highlightActiveLineGutter(),
      history(),
      drawSelection(),
      dropCursor(),
      indentOnInput(),
      bracketMatching(),
      highlightActiveLine(),
      python(),
      syntaxHighlighting(kstrHighlightStyle),
      semanticPlugin,
      hoverTooltip((view, pos) => kstrHover(state, view, pos), { hoverTime: 220, hideOnChange: true }),
      autocompletion({ override: [kstrCompletion], activateOnTyping: true, defaultKeymap: false }),
      Prec.highest(keymap.of(completionKeymap)),
      keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
      editorTheme,
      EditorView.updateListener.of((update) => {
        if (!update.docChanged) return;
        const text = update.state.doc.toString();
        if (state.sourceWidget.value !== text) state.sourceWidget.value = text;
        scheduleAnalyze(node, state);
      }),
    ],
  });
  state.view = new EditorView({ state: editorState, parent: shell.editorHost });

  // Comfy ChangeTracker exempts INPUT/TEXTAREA from workflow-level Ctrl+Z.
  // CodeMirror focuses a contenteditable DIV, so mark that focused element with
  // the same `type` discriminator ChangeTracker checks before it sees keydown.
  // This prevents workflow reload/rehydration at the source instead of trying to
  // stop an earlier window-capture listener after it already ran.
  state.view.contentDOM.type = "textarea";
  state.view.contentDOM.dataset.kstrFlowEditor = "true";

  // Analyze from the state closure immediately. Do not depend on node[STATE]
  // surviving Nodes 2.0 configure/rehydration before the first timer fires.
  queueMicrotask(() => {
    if (!state.destroyed) void analyzeState(node, state);
  });

  // Keep all editor interaction inside the DOM widget. Nodes 2.0 forwards wheel
  // events from the node container unless a focused descendant opts into wheel
  // capture; legacy mode also needs explicit scrolling instead of canvas zoom.
  const { signal } = controller;
  shell.editorHost.dataset.captureWheel = "true";
  shell.preview.dataset.captureWheel = "true";
  shell.preview.tabIndex = 0;
  bindInternalWheel(shell.editorHost, () => state.view?.scrollDOM, signal);
  bindInternalWheel(shell.preview, () => shell.preview, signal);
  for (const eventName of ["keydown", "keyup", "pointerdown", "pointerup", "mousedown", "mouseup", "click"]) {
    shell.editorHost.addEventListener(eventName, (event) => event.stopPropagation(), { signal });
  }
  for (const eventName of ["pointerdown", "pointerup", "mousedown", "mouseup"]) {
    shell.preview.addEventListener(eventName, (event) => event.stopPropagation(), { signal });
  }
  shell.preview.addEventListener("pointerdown", () => shell.preview.focus({ preventScroll: true }), { signal });

  shell.nodesButton.addEventListener("click", () => {
    if (state.browserOpen) closeNodeBrowser(state);
    else openNodeBrowser(state);
  });
  shell.browserClose.addEventListener("click", () => closeNodeBrowser(state));
  shell.browserSearch.addEventListener("input", () => renderNodeBrowser(state));
  shell.browserSearch.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); closeNodeBrowser(state); }
  });
  shell.root.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === "n") {
      event.preventDefault();
      if (state.browserOpen) closeNodeBrowser(state);
      else openNodeBrowser(state);
    }
  });

  const domWidget = node.addDOMWidget("kstr_flow_editor", "KSTR_FLOW_EDITOR", shell.root, {
    serialize: false,
    hideOnZoom: false,
    getValue: () => "",
    setValue: () => {},
    // Let LiteGraph allocate all remaining widget space. The DOM root then fills
    // that allocation instead of keeping an independent fixed pixel height.
    getMinHeight: () => 300,
    afterResize: () => {
      state.view?.requestMeasure();
    },
  });
  domWidget.options.minNodeSize = [520, 430];
  node.setSize([Math.max(node.size?.[0] ?? 520, 620), Math.max(node.size?.[1] ?? 430, 620)]);

  const oldRemoved = node.onRemoved;
  node.onRemoved = function (...args) {
    state.destroyed = true;
    controller.abort();
    state.analyzeController?.abort();
    state.view?.destroy();
    clearTimeout(state.timer);
    if (shell.root[ROOT_STATE] === state) delete shell.root[ROOT_STATE];
    if (node[STATE] === state) delete node[STATE];
    return oldRemoved?.apply(this, args);
  };

  // Preload registry so first completion/browser opening is instant.
  loadRegistry().then((registry) => { state.registry = registry; }).catch(() => {});
  scheduleAnalyze(node, state, 0);
}

function isFlowNode(node) {
  return node?.comfyClass === FLOW_CLASS || node?.type === FLOW_CLASS;
}

function ensureFlowNode(node) {
  if (!isFlowNode(node)) return;
  if (node[STATE]?.destroyed) delete node[STATE];
  install(node);
  syncEditorFromWidget(node);
  const state = node[STATE];
  if (state) scheduleAnalyze(node, state, 0);
}

function walkGraph(graph, visit) {
  if (!graph) return;
  for (const node of graph._nodes ?? []) visit(node);
  const subgraphs = graph.subgraphs;
  if (subgraphs?.values) for (const subgraph of subgraphs.values()) walkGraph(subgraph, visit);
}

app.registerExtension({
  name: "KSTR.Flow",
  async nodeCreated(node) {
    if (isFlowNode(node)) install(node);
  },
  loadedGraphNode(node) {
    ensureFlowNode(node);
  },
  async afterConfigureGraph() {
    // ChangeTracker undo/redo reloads the whole graph. Re-scan after every
    // configure pass so Nodes 2.0 rehydration cannot leave a dead editor behind.
    requestAnimationFrame(() => walkGraph(app.rootGraph, ensureFlowNode));
  },
});
