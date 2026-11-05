// @ts-check
// The triage graph (#118): draws a GraphView from trace-reducer.mjs `graphView()` as SVG.
// Dumb by design: every position, state and count comes from the view. Lit nodes and taken
// edges change colour through CSS transitions, which the reduced-motion rule turns off.
import { html } from "htm/preact";

/** @typedef {import("../trace-reducer.mjs").GraphView} GraphView */
/** @typedef {import("../trace-reducer.mjs").GraphNodeView} GraphNodeView */

/**
 * Activate on Enter or Space, as a button does.
 * @param {KeyboardEvent} event
 * @param {() => void} action
 */
function onKey(event, action) {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  action();
}

/**
 * One node: a box with its name in mono, or a small circle for __start__ and __end__.
 * @param {{ node: GraphNodeView, fontPx: number, onSelect?: (name: string) => void }} props
 */
function Node({ node, fontPx, onSelect }) {
  const select = onSelect && node.selectable ? () => onSelect(node.name) : undefined;
  const shape = node.terminal
    ? html`<circle class="graph__terminal" cx=${node.x} cy=${node.y} r=${node.r} />`
    : html`
        <rect
          class="graph__box"
          x=${node.x - node.width / 2}
          y=${node.y - node.height / 2}
          width=${node.width}
          height=${node.height}
          rx="4"
        />
        <text class="graph__name" x=${node.x} y=${node.y} font-size=${fontPx}>${node.name}</text>
      `;
  return html`
    <g
      class="graph__node"
      data-node=${node.name}
      data-state=${node.state}
      data-visits=${node.visits}
      role=${select ? "button" : undefined}
      tabindex=${select ? 0 : undefined}
      onClick=${select}
      onKeyDown=${select && ((/** @type {KeyboardEvent} */ e) => onKey(e, select))}
    >
      <title>${node.label}</title>
      ${shape}
    </g>
  `;
}

/**
 * The graph, with an optional hook for the node inspector (#121): when `onSelectNode` is
 * given, every node is a button that calls it with the node's name.
 * @param {{ view: GraphView, onSelectNode?: (name: string) => void }} props
 */
export function Graph({ view, onSelectNode }) {
  return html`
    <figure class="graph" data-graph="ready">
      <svg
        class="graph__svg"
        viewBox=${view.viewBox}
        role="group"
        aria-label=${view.label}
      >
        ${view.edges.map(
          (edge) => html`
            <g
              class="graph__edge"
              key=${edge.key}
              data-edge=${edge.key}
              data-source=${edge.source}
              data-target=${edge.target}
              data-route=${edge.route ?? undefined}
              data-taken=${String(edge.taken)}
            >
              <path class="graph__line" d=${edge.d} />
              <polygon class="graph__arrow" points=${edge.arrow} />
              ${edge.label &&
              html`<text class="graph__label" x=${edge.label.x} y=${edge.label.y} font-size=${view.labelPx}>
                ${edge.route}
              </text>`}
            </g>
          `,
        )}
        ${view.nodes.map(
          (node) =>
            html`<${Node} key=${node.name} node=${node} fontPx=${view.nodePx} onSelect=${onSelectNode} />`,
        )}
      </svg>
    </figure>
  `;
}
