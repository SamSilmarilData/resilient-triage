// @ts-check
// The node inspector (#121): a modal <dialog> with one graph node's purpose, file, config
// fields and ADRs, drawn from trace-reducer.mjs `inspectorView()`. The native dialog gives
// focus containment and Escape; on close, focus goes back to whatever opened it (a graph node
// or a button in the node list). Its head (title and Close) stays put while the body scrolls,
// and a click on the backdrop closes it like Close (#164).
import { html } from "htm/preact";
import { useEffect, useRef } from "preact/hooks";

/** @typedef {import("../trace-reducer.mjs").InspectorView} InspectorView */

/**
 * The inspector's body once the node list has loaded.
 * @param {{ view: InspectorView }} props
 */
function Facts({ view }) {
  return html`
    <p class="inspector__purpose" data-inspector-purpose>${view.purpose}</p>
    <dl class="inspector__facts">
      <dt>File</dt>
      <dd>
        <a data-source-link href=${view.file?.url}><code>${view.file?.path}</code></a>
      </dd>
      <dt>Config fields</dt>
      <dd>
        ${view.configFields.length
          ? html`<ul class="inspector__list">
              ${view.configFields.map(
                (field) => html`
                  <li key=${field.name} data-config-field=${field.name}>
                    <code>${field.name}</code>
                    <span class="inspector__note">${field.description}</span>
                  </li>
                `,
              )}
            </ul>`
          : html`<span class="inspector__note">None: nothing in Settings tunes this node.</span>`}
      </dd>
      <dt>Decision records</dt>
      <dd>
        <ul class="inspector__list">
          ${view.adrs.map(
            (adr) => html`
              <li key=${adr.file}>
                <a data-adr-link href=${adr.url}>${adr.label}</a>
                <span class="inspector__note"><code>${adr.file}</code></span>
              </li>
            `,
          )}
        </ul>
      </dd>
    </dl>
  `;
}

/**
 * @param {{ view: InspectorView | null, onClose: () => void }} props
 * `view` null closes the dialog; the dialog calls `onClose` however it was closed.
 */
export function NodeInspector({ view, onClose }) {
  const dialog = useRef(/** @type {HTMLDialogElement | null} */ (null));
  const opener = useRef(/** @type {Element | null} */ (null));
  const open = view !== null;

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) {
      opener.current = document.activeElement;
      element.showModal();
    } else if (!open && element.open) {
      element.close();
    }
  }, [open]);

  // --- #164: backdrop close ---
  // The dialog has no padding and its frame fills it, so a press whose target is the dialog
  // itself landed on the backdrop. Both ends of the click must: a text selection dragged out
  // of the panel is not a backdrop click.
  const pressedBackdrop = useRef(false);
  /** @param {PointerEvent} event */
  const pressed = (event) => {
    pressedBackdrop.current = event.target === event.currentTarget;
  };
  /** @param {MouseEvent} event */
  const clicked = (event) => {
    if (pressedBackdrop.current && event.target === event.currentTarget) {
      dialog.current?.close();
    }
    pressedBackdrop.current = false;
  };

  const closed = () => {
    const back = opener.current;
    opener.current = null;
    if (back instanceof HTMLElement || back instanceof SVGElement) {
      if (back.isConnected) back.focus();
    }
    onClose();
  };

  return html`
    <dialog
      ref=${dialog}
      class="inspector"
      aria-labelledby="inspector-title"
      data-inspected=${view?.name}
      onClose=${closed}
      onPointerDown=${pressed}
      onClick=${clicked}
    >
      ${view &&
      html`
        <div class="inspector__frame">
        <div class="inspector__head">
          <div>
            <p class="eyebrow">Graph node</p>
            <h2 id="inspector-title" class="inspector__title">${view.name}</h2>
          </div>
          <button class="button" type="button" onClick=${() => dialog.current?.close()}>
            Close
          </button>
        </div>
        <div class="inspector__body">
        ${view.status === "ready"
          ? html`<${Facts} view=${view} />`
          : view.status === "loading"
            ? html`<p class="inspector__note">Loading this node's details.</p>`
            : html`<p class="notice">
                This node's details didn't load. The list under How it's built can try again.
              </p>`}
        </div>
        </div>
      `}
    </dialog>
  `;
}
