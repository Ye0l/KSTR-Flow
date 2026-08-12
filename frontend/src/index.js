import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

import { autocompletion, completeFromList } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { bracketMatching, indentOnInput } from "@codemirror/language";
import { setDiagnostics } from "@codemirror/lint";
import { searchKeymap } from "@codemirror/search";
import { EditorState } from "@codemirror/state";
import {
  EditorView,
  drawSelection,
  dropCursor,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";

const FLOW_CLASS = "KSTRFlow";
const STATE = Symbol("kstrFlowState");
const BUILTINS = [
  "abs", "all", "any", "bool", "dict", "enumerate", "filter", "float",
  "int", "len", "list", "map", "max", "min", "range", "reversed",
  "round", "set", "sorted", "str", "sum", "tuple", "zip",
];
const RESERVED = ["global_seed", "seed", "random", "math", "nodes"];

let registryPromise = null;

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
  return node.inputs?.find((slot) => slot?._kstrFlow && slot.name === name);
}

function syncInputs(node, ports) {
  const wanted = new Set(ports.map((port) => port.name));
  for (let index = (node.inputs?.length ?? 0) - 1; index >= 0; index--) {
    const slot = node.inputs[index];
    if (slot?._kstrFlow && !wanted.has(slot.name)) node.removeInput(index);
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
  const outputs = (nodeInfo.outputs ?? []).map((output) => output.type).join(", ") || "None";
  return `(${args}) → ${outputs}`;
}

function makeNodeCompletion(nodeInfo) {
  const flags = [nodeInfo.deprecated && "deprecated", nodeInfo.experimental && "experimental"].filter(Boolean);
  return {
    label: nodeInfo.name,
    type: "function",
    detail: signatureText(nodeInfo),
    info: [nodeInfo.description, flags.length ? `Flags: ${flags.join(", ")}` : null].filter(Boolean).join("\n\n"),
    apply: `${nodeInfo.name}(`,
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
  const node = registry.packs?.[pack]?.find((item) => item.name === match[2]);
  return node ? { node, fragment: match[3] } : null;
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
    const used = new Set([...call.fragment.matchAll(/\b([A-Za-z_]\w*)\s*=/g)].map((m) => m[1]));
    const word = context.matchBefore(/[A-Za-z_]*$/);
    const options = call.node.inputs
      .filter((input) => !used.has(input.name))
      .map((input) => ({
        label: `${input.name}=`,
        type: "property",
        detail: `${input.type}${input.optional ? " optional" : ""}${input.has_default ? ` = ${JSON.stringify(input.default)}` : ""}`,
        info: input.tooltip || (input.options?.length ? input.options.join("\n") : ""),
        apply: `${input.name}=`,
      }));
    if (options.length) return { from: word.from, options };
  }

  const word = context.matchBefore(/[A-Za-z_]\w*$/);
  if (!word || (!context.explicit && word.from === word.to)) return null;
  return {
    from: word.from,
    options: [
      ...RESERVED.map((label) => ({ label, type: "variable", detail: "KSTR Flow runtime" })),
      ...BUILTINS.map((label) => ({ label, type: "function", detail: "safe builtin" })),
    ],
  };
}

const editorTheme = EditorView.theme({
  "&": {
    height: "100%",
    minHeight: "260px",
    fontSize: "12px",
    background: "var(--comfy-input-bg, #18181b)",
    color: "var(--fg-color, #ddd)",
  },
  ".cm-content": { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", padding: "8px 0" },
  ".cm-scroller": { overflow: "auto" },
  ".cm-gutters": { background: "transparent", border: "none", color: "#777" },
  ".cm-activeLine, .cm-activeLineGutter": { background: "rgba(127,127,127,.09)" },
  ".cm-tooltip-autocomplete": { zIndex: "10000" },
});

function errorDiagnostic(view, error) {
  if (!error) return [];
  const lineNo = Math.max(1, Number(error.line || 1));
  const line = view.state.doc.line(Math.min(lineNo, view.state.doc.lines));
  const col = Math.max(0, Number(error.column || 1) - 1);
  const from = Math.min(line.to, line.from + col);
  return [{ from, to: Math.min(line.to, from + 1), severity: "error", message: error.message || String(error) }];
}

function makeShell() {
  const root = document.createElement("div");
  root.className = "kstr-flow-shell";
  Object.assign(root.style, {
    display: "grid",
    gridTemplateRows: "minmax(110px, 0.42fr) 6px minmax(260px, 1fr)",
    height: "520px",
    minHeight: "420px",
    width: "100%",
    overflow: "hidden",
    border: "1px solid rgba(127,127,127,.25)",
    borderRadius: "6px",
    background: "rgba(20,20,22,.94)",
  });

  const preview = document.createElement("div");
  preview.className = "kstr-flow-preview";
  Object.assign(preview.style, { position: "relative", overflow: "auto", padding: "8px", userSelect: "none" });
  preview.innerHTML = `<div style="opacity:.6;font:12px ui-monospace,monospace">Graph preview · waiting for analysis</div>`;

  const divider = document.createElement("div");
  Object.assign(divider.style, { background: "rgba(127,127,127,.18)" });

  const editorHost = document.createElement("div");
  Object.assign(editorHost.style, { minHeight: "0", overflow: "hidden" });

  root.append(preview, divider, editorHost);
  return { root, preview, editorHost };
}

function renderPreview(state) {
  const { preview, analysis } = state;
  if (!analysis) {
    preview.innerHTML = `<div style="opacity:.6;font:12px ui-monospace,monospace">Graph preview · waiting for analysis</div>`;
    return;
  }
  if (!analysis.ok) {
    preview.innerHTML = `<div style="color:#e57373;font:12px ui-monospace,monospace">${escapeHtml(analysis.error?.message || "Syntax error")}</div>`;
    return;
  }
  const inputText = (analysis.inputs ?? []).map((p) => `${p.name}:${p.type}`).join(" · ") || "no external inputs";
  const outputText = (analysis.outputs ?? []).map((p) => `${p.name}:${p.type}`).join(" · ") || "no outputs";
  preview.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:7px;font:12px ui-monospace,monospace">
      <div style="opacity:.65">Graph preview · graph IR next</div>
      <div><b>IN</b> ${escapeHtml(inputText)}</div>
      <div><b>OUT</b> ${escapeHtml(outputText)}</div>
    </div>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[ch]));
}

async function analyze(node) {
  const state = node[STATE];
  if (!state?.view) return;
  const source = state.view.state.doc.toString();
  const generation = ++state.generation;

  try {
    const response = await api.fetchApi("/kstr-flow/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source }),
    });
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
    state.error = { message: String(error), line: 1, column: 1 };
    setDiagnostics(state.view, errorDiagnostic(state.view, state.error));
    renderPreview(state);
  }
}

function scheduleAnalyze(node, delay = 180) {
  const state = node[STATE];
  if (!state) return;
  clearTimeout(state.timer);
  state.timer = setTimeout(() => analyze(node), delay);
}

function install(node) {
  if (node[STATE]) return;

  const source = sourceWidget(node);
  if (!source) return;
  setWidgetHidden(source, true);

  const shell = makeShell();
  const state = node[STATE] = {
    generation: 0,
    timer: null,
    analysis: null,
    error: null,
    sourceWidget: source,
    preview: shell.preview,
    view: null,
  };

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
      autocompletion({ override: [kstrCompletion], activateOnTyping: true }),
      keymap.of([...defaultKeymap, ...historyKeymap, ...searchKeymap, indentWithTab]),
      editorTheme,
      EditorView.updateListener.of((update) => {
        if (!update.docChanged) return;
        const text = update.state.doc.toString();
        if (state.sourceWidget.value !== text) state.sourceWidget.value = text;
        scheduleAnalyze(node);
      }),
    ],
  });
  state.view = new EditorView({ state: editorState, parent: shell.editorHost });

  const domWidget = node.addDOMWidget("kstr_flow_editor", "KSTR_FLOW_EDITOR", shell.root, {
    serialize: false,
    hideOnZoom: false,
    getValue: () => "",
    setValue: () => {},
  });
  domWidget.options.minNodeSize = [520, 620];
  node.setSize([Math.max(node.size?.[0] ?? 520, 620), Math.max(node.size?.[1] ?? 620, 720)]);

  const oldRemoved = node.onRemoved;
  node.onRemoved = function (...args) {
    state.view?.destroy();
    clearTimeout(state.timer);
    return oldRemoved?.apply(this, args);
  };

  // Preload registry so first completion is instant after initial analysis.
  loadRegistry().catch(() => {});
  scheduleAnalyze(node, 0);
}

app.registerExtension({
  name: "KSTR.Flow",
  async nodeCreated(node) {
    if (node.comfyClass === FLOW_CLASS) install(node);
  },
  loadedGraphNode(node) {
    if (node.comfyClass === FLOW_CLASS) {
      install(node);
      scheduleAnalyze(node, 0);
    }
  },
});
