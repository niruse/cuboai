// Stand-ins for the Home Assistant frontend pieces the CuboAI cards use, so the
// REAL cuboai-card.js can render outside Home Assistant with sample data.

import * as mdi from "https://cdn.jsdelivr.net/npm/@mdi/js@7.4.47/+esm";

// A fixed clock: every image shows the same night, whenever it is rendered.
export const FIXED_NOW = new Date("2026-09-20T07:30:00").getTime();
const RealDate = Date;
const t0 = performance.now();
class FrozenDate extends RealDate {
  constructor(...args) {
    if (args.length === 0) super(FIXED_NOW + (performance.now() - t0));
    else super(...args);
  }
  static now() {
    return FIXED_NOW + (performance.now() - t0);
  }
}
window.Date = FrozenDate;

export function mdiPath(icon) {
  const name = String(icon || "").replace(/^mdi:/, "");
  const key = "mdi" + name.split("-").map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join("");
  return mdi[key] || mdi.mdiHelpCircleOutline;
}

class HaIcon extends HTMLElement {
  static get observedAttributes() {
    return ["icon"];
  }
  set icon(v) {
    this.setAttribute("icon", v);
  }
  get icon() {
    return this.getAttribute("icon");
  }
  connectedCallback() {
    this._draw();
  }
  attributeChangedCallback() {
    this._draw();
  }
  _draw() {
    this.innerHTML = `<svg viewBox="0 0 24 24"><path d="${mdiPath(this.getAttribute("icon"))}"/></svg>`;
  }
}
customElements.define("ha-icon", HaIcon);

class HaIconButton extends HTMLElement {
  connectedCallback() {
    this.style.display = "inline-flex";
    this.style.cursor = "pointer";
  }
}
customElements.define("ha-icon-button", HaIconButton);

// ha-card draws its `header` as the card title, above the content.
class HaCard extends HTMLElement {
  set header(v) {
    this._header = v;
    let h = this.querySelector(":scope > .card-header");
    if (!v) {
      if (h) h.remove();
      return;
    }
    if (!h) {
      h = document.createElement("div");
      h.className = "card-header";
      h.style.cssText = "font: 400 24px/1.2 Roboto, sans-serif; padding: 20px 16px 4px; color: var(--primary-text-color)";
      this.prepend(h);
    }
    h.textContent = v;
  }
  get header() {
    return this._header;
  }
}
customElements.define("ha-card", HaCard);

// The camera picture, drawn: a night-vision view down into a crib. No photo,
// no person — just bars, a mattress and a blanket in infrared grey.
export const NURSERY_SVG = `
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360" preserveAspectRatio="xMidYMid slice">
  <defs>
    <radialGradient id="glow" cx="50%" cy="55%" r="65%">
      <stop offset="0" stop-color="#8d949a"/><stop offset="0.6" stop-color="#4c5257"/><stop offset="1" stop-color="#1f2326"/>
    </radialGradient>
    <linearGradient id="blanket" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#b9bec2"/><stop offset="1" stop-color="#7d8388"/>
    </linearGradient>
    <pattern id="knit" width="14" height="14" patternUnits="userSpaceOnUse">
      <path d="M0 7 Q3.5 3 7 7 T14 7" fill="none" stroke="#6f757a" stroke-width="1.2" opacity=".45"/>
    </pattern>
  </defs>
  <rect width="640" height="360" fill="url(#glow)"/>
  <rect x="120" y="70" width="400" height="250" rx="18" fill="#a3a8ac" opacity=".55"/>
  <rect x="150" y="95" width="340" height="205" rx="14" fill="#c4c8cb" opacity=".55"/>
  <ellipse cx="250" cy="150" rx="62" ry="36" fill="#d3d6d8" opacity=".75"/>
  <path d="M190 190 Q320 150 470 200 L470 300 L170 300 Z" fill="url(#blanket)"/>
  <path d="M190 190 Q320 150 470 200 L470 300 L170 300 Z" fill="url(#knit)"/>
  <g fill="#2a2f33" opacity=".85">
    ${Array.from({ length: 13 }, (_, i) => `<rect x="${118 + i * 33}" y="40" width="9" height="300" rx="4"/>`).join("")}
  </g>
  <rect x="100" y="30" width="440" height="16" rx="8" fill="#353a3e"/>
  <rect width="640" height="360" fill="#000" opacity=".08"/>
</svg>`;

// webrtc-camera (AlexxIT) stand-in: shows the drawn nursery view where the
// live video would be. The card only configures it and places overlays on it.
class WebrtcCamera extends HTMLElement {
  setConfig(config) {
    this._config = config;
  }
  set hass(h) {
    this._hass = h;
  }
  connectedCallback() {
    if (this._drawn) return;
    this._drawn = true;
    this.style.display = "block";
    this.style.position = "relative";
    this.innerHTML = `
      <div style="position:relative;width:100%;aspect-ratio:16/9;background:#1f2326;overflow:hidden">
        <div style="position:absolute;inset:0">${NURSERY_SVG}</div>
        <div style="position:absolute;left:10px;bottom:8px;display:flex;gap:10px;color:#fff;opacity:.85">
        </div>
      </div>`;
    const svg = this.querySelector("svg");
    svg.style.width = "100%";
    svg.style.height = "100%";
    // A video whose clock advances, so the card's "is the image frozen?"
    // timestamp badge sees a live stream (a stub that never plays turns it red).
    this.video = document.createElement("video");
    Object.defineProperty(this.video, "currentTime", { get: () => performance.now() / 1000 });
    Object.defineProperty(this.video, "readyState", { get: () => 4 });
  }
  getCardSize() {
    return 3;
  }
}
customElements.define("webrtc-camera", WebrtcCamera);

window.customCards = window.customCards || [];
