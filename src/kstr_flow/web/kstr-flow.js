import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const FLOW_CLASS = "KSTRFlow";
const STATE = Symbol("kstrFlowState");

function sourceWidget(node) {
  return node.widgets?.find((widget) => widget.name === "source");
}

function normalizeType(type) {
  if (!type || type === "ANY" || type === "typing.Any") return "*";
  // Annotations such as tuple[IMAGE, MASK] are not socket types. Output tuple
  // annotations are already split by the backend, so fall back safely here.
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
    if (slot?._kstrFlow && !wanted.has(slot.name)) {
      node.removeInput(index);
    }
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

  // Backend exposes a fixed AnyType capacity. The frontend only shows the
  // outputs declared by the script; output indexes remain stable.
  while ((node.outputs?.length ?? 0) > ports.length) {
    node.removeOutput(node.outputs.length - 1);
  }
}

async function analyze(node) {
  const state = node[STATE];
  if (!state) return;
  const widget = sourceWidget(node);
  if (!widget) return;
  const source = String(widget.value ?? "");
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
    node.setDirtyCanvas?.(true, true);
  } catch (error) {
    if (generation !== state.generation) return;
    state.error = { message: String(error) };
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
  node[STATE] = { generation: 0, timer: null, analysis: null, error: null };
  const widget = sourceWidget(node);
  if (widget) {
    const original = widget.callback;
    widget.callback = function (...args) {
      const result = original?.apply(this, args);
      scheduleAnalyze(node);
      return result;
    };
  }
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
