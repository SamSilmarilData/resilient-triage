// @ts-check
// A load that failed (#101): one alert saying what didn't load, with a Retry button. The
// chapters (GET /scenarios) and How it's built's node list (GET /graph/nodes) both use it.
import { html } from "htm/preact";

/** @param {{ message: string, onRetry: () => void }} props */
export function RetryNotice({ message, onRetry }) {
  return html`<div class="notice notice--action" role="alert">
    <p>${message}</p>
    <button class="button" type="button" onClick=${onRetry}>Retry</button>
  </div>`;
}
