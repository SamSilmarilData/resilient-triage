// @ts-check
// How it's built (#121): the section's prose is static HTML in index.html (#101); this draws
// only the list of graph nodes from GET /graph/nodes, each a button that opens the node
// inspector. `data-graph-nodes` on the list's root is the load state; browser tests wait on it.
import { html } from "htm/preact";

/**
 * @template T
 * @typedef {import("../trace-reducer.mjs").Loadable<T>} Loadable
 */
/** @typedef {import("../trace-reducer.mjs").GraphNode} GraphNode */

/**
 * @param {{
 *   nodes: Loadable<GraphNode[]>,
 *   onSelect: (name: string) => void,
 *   onRetry: () => void,
 * }} props
 */
export function NodeList({ nodes, onSelect, onRetry }) {
  if (nodes === "loading") return html`<div class="built__nodes" data-graph-nodes="loading"></div>`;
  if (nodes === "failed") {
    return html`
      <div class="built__nodes" data-graph-nodes="failed">
        <p class="notice">
          The node list didn't load.
          <button class="button" type="button" onClick=${onRetry}>Retry</button>
        </p>
      </div>
    `;
  }
  return html`
    <div class="built__nodes" data-graph-nodes="ready">
      <ul class="built__node-list">
        ${nodes.map(
          (node) => html`
            <li key=${node.name} data-built-node=${node.name}>
              <button class="built__node" type="button" onClick=${() => onSelect(node.name)}>
                ${node.name}
              </button>
              <span class="built__purpose">${node.purpose}</span>
            </li>
          `,
        )}
      </ul>
    </div>
  `;
}
