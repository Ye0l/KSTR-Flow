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
const STATIC_INPUTS = new Set(["source", "global_seed"]);
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
    minHeight: "0",
    fontSize: "12px",
    background: "var(--comfy-input-bg, #18181b)",
    color: "var(--fg-color, #ddd)",
  },
  ".cm-content": { fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", padding: "8px 0" },
  ".cm-scroller": { overflow: "auto", minHeight: "0" },
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
  });

  const preview = document.createElement("div");
  preview.className = "kstr-flow-preview";
  Object.assign(preview.style, { position: "relative", overflow: "auto", padding: "8px", userSelect: "none" });
  preview.innerHTML = `<div style="opacity:.6;font:12px ui-monospace,monospace">Graph preview · waiting for analysis</div>`;

  const divider = document.createElement("div");
  Object.assign(divider.style, { background: "rgba(127,127,127,.18)" });

  const editorHost = document.createElement("div");
  Object.assign(editorHost.style, { minHeight: "0", minWidth: "0", overflow: "hidden" });

  root.append(preview, divider, editorHost);
  return { root, preview, editorHost };
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
  if (!analysis) {
    const waiting = document.createElement("div");
    waiting.textContent = "Graph preview · waiting for analysis";
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
      syncEditorFromWidget(node);
      scheduleAnalyze(node, 0);
    }
  },
});
