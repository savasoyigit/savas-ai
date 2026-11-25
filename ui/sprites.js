// K-3d: SAVAS AI's own art (plan-k karar C): characters and furniture are flat
// vector SVG (first "karışık" with pixel furniture, then all vector at the user's
// request). No image files.

const OUT = "#14102a";
const LINE = `stroke="${OUT}" stroke-width="2" stroke-linejoin="round"`;

const eyes = (y = 27) => `
  <ellipse cx="27" cy="${y}" rx="2.1" ry="2.7" fill="${OUT}"/>
  <ellipse cx="37" cy="${y}" rx="2.1" ry="2.7" fill="${OUT}"/>
  <circle cx="27.8" cy="${y - 1}" r=".8" fill="#fff"/>
  <circle cx="37.8" cy="${y - 1}" r=".8" fill="#fff"/>`;
const smile = (wide = false) => wide
  ? `<path d="M28 33 Q32 37.5 36 33 Z" fill="#8a1c3d" ${LINE} stroke-width="1.4"/>`
  : `<path d="M29 33.2 Q32 35.6 35 33.2" fill="none" stroke="${OUT}" stroke-width="1.6" stroke-linecap="round"/>`;
const cheeks = `<circle cx="23.5" cy="31" r="2.2" fill="#ff7a9a" opacity=".35"/><circle cx="40.5" cy="31" r="2.2" fill="#ff7a9a" opacity=".35"/>`;

// One figure: back layer, body, neck, head, face, front layer.
function figure({ skin = "#f1c27d", shirt, shirtDark, back = "", head = null, face = null, front = "", collar = "" }) {
  return `
  ${back}
  <path d="M15 64 C15 50 22 42.5 32 42.5 C42 42.5 49 50 49 64 Z" fill="${shirt}" ${LINE}/>
  <path d="M24 45 Q32 52 40 45" fill="none" stroke="${shirtDark}" stroke-width="2.4" stroke-linecap="round"/>
  ${collar}
  <rect x="28" y="36" width="8" height="8" rx="3" fill="${skin}"/>
  ${head ?? `<ellipse cx="32" cy="26" rx="13" ry="13.5" fill="${skin}" ${LINE}/>`}
  ${face ?? eyes() + smile() + cheeks}
  ${front}`;
}

export const CHARACTERS = {
  ares: {
    color: "#8b5cff",
    svg: figure({
      shirt: "#5a2fd6", shirtDark: "#3a1d99",
      back: `<path d="M13 64 L18 44 L46 44 L51 64 Z" fill="#ff3b5c" ${LINE}/>`,
      front: `
        <path d="M18 29 C17 15 23 10 32 10 C41 10 47 15 46 29 L46 33 L41.5 33 L41.5 22.5 L22.5 22.5 L22.5 33 L18 33 Z" fill="#7b4dff" ${LINE}/>
        <path d="M22.5 22.5 L41.5 22.5" stroke="#b9a2ff" stroke-width="1.6"/>
        <path d="M20 12.5 Q32 -4 44 12.5 Q32 6 20 12.5 Z" fill="#ff3b5c" ${LINE}/>
        <circle cx="32" cy="50.5" r="2.6" fill="#ffd23f" ${LINE} stroke-width="1.4"/>`,
    }),
  },
  gandalf: {
    color: "#b8c0d4",
    svg: figure({
      shirt: "#8d95a8", shirtDark: "#5c6378",
      face: eyes(26.5),
      front: `
        <path d="M20.5 29 Q21 45 32 51 Q43 45 43.5 29 Q38 34.5 32 34.5 Q26 34.5 20.5 29 Z" fill="#f4f4f4" ${LINE}/>
        <path d="M26 33 Q32 30 38 33" fill="none" stroke="#c9ccd6" stroke-width="2" stroke-linecap="round"/>
        <ellipse cx="32" cy="17.5" rx="19.5" ry="4.6" fill="#7d8597" ${LINE}/>
        <path d="M20.5 17.5 Q27 9 29.5 1.5 Q33 -0.5 38.5 4 Q35 5.8 34.5 9 Q40.5 13 43.5 17.5 Z" fill="#9aa3b5" ${LINE}/>
        <path d="M22.5 15.6 Q32 18 41.5 15.6" fill="none" stroke="#5c6378" stroke-width="2"/>`,
    }),
  },
  jarvis: {
    color: "#26e0ff",
    svg: figure({
      skin: "#d9a066", shirt: "#1aa6c9", shirtDark: "#0d6f88",
      face: smile(),
      front: `
        <path d="M19 25 C18.5 13 25 10.5 32 10.5 C40 10.5 46 13.5 45 25 C41 19 35.5 17.5 30.5 18 C25.5 18.5 22 21 19 25 Z" fill="#222a3d" ${LINE}/>
        <rect x="19.5" y="22.5" width="25" height="7.5" rx="3.75" fill="#26e0ff" ${LINE}/>
        <path d="M23 25 L33 25" stroke="#d6fbff" stroke-width="1.6" stroke-linecap="round"/>
        <circle cx="18.5" cy="28" r="3.2" fill="#0d6f88" ${LINE} stroke-width="1.6"/>
        <path d="M18.5 31 Q19 37 25 37.5" fill="none" stroke="${OUT}" stroke-width="1.4"/>`,
      collar: `<path d="M26 43 L32 49 L38 43" fill="none" stroke="#d6fbff" stroke-width="2" stroke-linejoin="round"/>`,
    }),
  },
  oracle: {
    color: "#14d1b5",
    svg: figure({
      skin: "#c68642", shirt: "#0e8f7f", shirtDark: "#08594f",
      back: `<path d="M14 42 C11.5 18 20 8 32 8 C44 8 52.5 18 50 42 Z" fill="#10b5a0" ${LINE}/>`,
      front: `
        <path d="M19 31 C18.5 17 24.5 12.5 32 12.5 C39.5 12.5 45.5 17 45 31 C44 22.5 39 18 32 18 C25 18 20 22.5 19 31 Z" fill="#0e8f7f"/>
        <path d="M32 13.5 L35.2 17.2 L32 21 L28.8 17.2 Z" fill="#ffd23f" ${LINE} stroke-width="1.4"/>`,
    }),
  },
  willy: {
    color: "#ff4fa3",
    svg: figure({
      shirt: "#ff7ab8", shirtDark: "#c2185b",
      face: eyes() + smile(true) + cheeks,
      back: `<path d="M17.5 33 C15 15 23 10.5 32 10.5 C41 10.5 49 15 46.5 33 Z" fill="#6b3e26" ${LINE}/>`,
      front: `
        <path d="M19.5 25 C21 18 26 16 32 16.5 C38 16 43 18 44.5 25 C40 21 35 20 32 21 C29 20 24 21 19.5 25 Z" fill="#6b3e26"/>
        <ellipse cx="30" cy="13.5" rx="15.5" ry="6" transform="rotate(-10 30 13.5)" fill="#ff4fa3" ${LINE}/>
        <circle cx="29" cy="7" r="2.2" fill="#c2185b" ${LINE} stroke-width="1.4"/>
        <path d="M44 52 L52 43" stroke="#8a5a2b" stroke-width="2.6" stroke-linecap="round"/>
        <path d="M51 44 L54 40.5" stroke="#26e0ff" stroke-width="3" stroke-linecap="round"/>`,
    }),
  },
  gordon: {
    color: "#ff4d5e",
    svg: figure({
      skin: "#e0ac69", shirt: "#b5232f", shirtDark: "#7a1820",
      face: `
        <circle cx="27" cy="27" r="4.4" fill="#ffffff66" ${LINE} stroke-width="1.6"/>
        <circle cx="37" cy="27" r="4.4" fill="#ffffff66" ${LINE} stroke-width="1.6"/>
        <path d="M31.4 27 L32.6 27" stroke="${OUT}" stroke-width="1.6"/>
        <circle cx="27" cy="27" r="1.6" fill="${OUT}"/><circle cx="37" cy="27" r="1.6" fill="${OUT}"/>
        ${smile()}`,
      front: `
        <path d="M19 22.5 C19 12.5 25 9.5 32 9.5 C39 9.5 45 12.5 45 22.5 Z" fill="#e63946" ${LINE}/>
        <path d="M40 21.5 Q50.5 20 54 23.5 Q47.5 26 40 24.5 Z" fill="#a4161a" ${LINE}/>
        <circle cx="32" cy="10.5" r="1.6" fill="#a4161a"/>
        <rect x="36.5" y="47" width="11" height="14" rx="1.6" fill="#f1e3c8" ${LINE} stroke-width="1.6"/>
        <rect x="39.5" y="45.5" width="5" height="3" rx="1" fill="#6b7590"/>
        <path d="M39 52 L45 52 M39 55.5 L45 55.5" stroke="#6b7590" stroke-width="1.2"/>`,
    }),
  },
  obsidian: {
    color: "#4d8bff",
    svg: figure({
      shirt: "#2f5fd0", shirtDark: "#1d3c8a",
      head: `
        <path d="M32 11 L32 5" stroke="${OUT}" stroke-width="2"/>
        <circle cx="32" cy="4.5" r="3" fill="#ffd23f" ${LINE} stroke-width="1.6"/>
        <rect x="16.5" y="11" width="31" height="26" rx="8" fill="#3d7bff" ${LINE}/>
        <rect x="13.5" y="20" width="4" height="8" rx="1.6" fill="#1d3c8a" ${LINE} stroke-width="1.4"/>
        <rect x="46.5" y="20" width="4" height="8" rx="1.6" fill="#1d3c8a" ${LINE} stroke-width="1.4"/>`,
      face: `
        <rect x="21" y="16.5" width="22" height="14.5" rx="5" fill="#0d1b4d"/>
        <rect x="24.5" y="20.5" width="5" height="5" rx="2" fill="#8ef3ff"/>
        <rect x="34.5" y="20.5" width="5" height="5" rx="2" fill="#8ef3ff"/>
        <path d="M28 28 L36 28" stroke="#8ef3ff" stroke-width="1.4" stroke-linecap="round"/>`,
      front: `<rect x="26" y="48" width="12" height="8" rx="2" fill="#0d1b4d"/><circle cx="29.5" cy="52" r="1.3" fill="#51cf66"/><circle cx="34.5" cy="52" r="1.3" fill="#ffd23f"/>`,
    }),
  },
};

// A worker added later: a plain figure in a colour from its id.
function generic(id) {
  let hash = 0;
  for (const ch of id) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  const hue = hash % 360;
  return {
    color: `hsl(${hue} 80% 65%)`,
    svg: figure({
      shirt: `hsl(${hue} 70% 50%)`, shirtDark: `hsl(${hue} 70% 32%)`,
      front: `<path d="M19 24 C19 12.5 25.5 10.5 32 10.5 C39 10.5 45 12.5 45 24 C40 18.5 35 17.5 32 18 C29 17.5 24 18.5 19 24 Z" fill="hsl(${hue} 45% 28%)" ${LINE}/>`,
    }),
  };
}

export function character(id) {
  return CHARACTERS[id] || generic(id);
}

const NS = "http://www.w3.org/2000/svg";

export function characterSvg(id) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 64 64");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "sprite");
  svg.innerHTML = character(id).svg; // constant markup from this file, never data
  return svg;
}

// Furniture: decoration only; it carries no data. Flat vector like the characters
// (K-3d follow-up, user request). Proportions match the old pixel maps so the
// room layout in app.js (DECOR) stays as it was.
const THIN = `stroke="${OUT}" stroke-width="1.6" stroke-linejoin="round"`;
const WOOD = "#c98a4b";
const WOOD_DARK = "#8a5a2b";

const FURNITURE = {
  desk: ["0 0 64 40", `
    <rect x="20" y="2" width="24" height="16" rx="2.5" fill="#2b3350" ${THIN}/>
    <rect x="23" y="5" width="18" height="10" rx="1.5" fill="#26e0ff"/>
    <path d="M25.5 8 H33 M25.5 11 H37" stroke="#0d6f88" stroke-width="1.4" stroke-linecap="round"/>
    <rect x="29.5" y="18" width="5" height="4" fill="#1b2036"/>
    <rect x="2" y="21" width="60" height="6" rx="2" fill="${WOOD}" ${THIN}/>
    <rect x="24" y="22.2" width="16" height="2.4" rx="1" fill="#e4e8f4"/>
    <rect x="49" y="17" width="5" height="5" rx="1.2" fill="#ff4d5e" ${THIN}/>
    <rect x="6" y="27" width="4.5" height="12" rx="1.2" fill="${WOOD_DARK}" ${THIN}/>
    <rect x="53.5" y="27" width="4.5" height="12" rx="1.2" fill="${WOOD_DARK}" ${THIN}/>
    <rect x="10.5" y="27" width="43" height="3" fill="${WOOD_DARK}"/>`],
  plant: ["0 0 32 36", `
    <path d="M16 22 C9 20 4 13 6 6 C11 9 14 14 16 22 Z" fill="#2f9e44" ${THIN}/>
    <path d="M16 22 C23 20 28 13 26 6 C21 9 18 14 16 22 Z" fill="#40c057" ${THIN}/>
    <path d="M16 22 C13 14 14 6 16 1 C18 6 19 14 16 22 Z" fill="#51cf66" ${THIN}/>
    <path d="M8 22 H24 L22 34 H10 Z" fill="#e8590c" ${THIN}/>
    <rect x="7" y="21" width="18" height="4" rx="1.5" fill="#c2410c" ${THIN}/>`],
  shelf: ["0 0 48 28", `
    <rect x="1" y="1" width="46" height="26" rx="2" fill="${WOOD_DARK}" ${THIN}/>
    <rect x="3.5" y="3.5" width="41" height="9" rx="1" fill="#5c3b1e"/>
    <rect x="3.5" y="15.5" width="41" height="9" rx="1" fill="#5c3b1e"/>
    <rect x="5" y="4.5" width="4" height="8" rx=".8" fill="#e03131"/><rect x="9.5" y="5.5" width="3.5" height="7" rx=".8" fill="#3d7bff"/>
    <rect x="13.5" y="4" width="4" height="8.5" rx=".8" fill="#fab005"/><rect x="18" y="5" width="3" height="7.5" rx=".8" fill="#2f9e44"/>
    <path d="M23 12.5 L27 4.5 L30 5.8 L26 12.5 Z" fill="#8b5cff"/>
    <rect x="32" y="6" width="4" height="6.5" rx=".8" fill="#14d1b5"/><rect x="36.5" y="4.5" width="4" height="8" rx=".8" fill="#ff4fa3"/>
    <rect x="5" y="17" width="3.5" height="7.5" rx=".8" fill="#3d7bff"/><rect x="9" y="16.5" width="4" height="8" rx=".8" fill="#2f9e44"/>
    <rect x="13.5" y="18" width="3.5" height="6.5" rx=".8" fill="#e03131"/><rect x="17.5" y="16.5" width="4" height="8" rx=".8" fill="#fab005"/>
    <circle cx="33" cy="21" r="3.2" fill="#74c0fc" ${THIN}/><rect x="39" y="17.5" width="3.5" height="7" rx=".8" fill="#8b5cff"/>`],
  rack: ["0 0 32 40", `
    <rect x="2" y="1" width="28" height="38" rx="3" fill="#2b2d42" ${THIN}/>
    <rect x="5" y="4.5" width="22" height="9" rx="1.5" fill="#3b3f5c"/>
    <rect x="5" y="15.5" width="22" height="9" rx="1.5" fill="#3b3f5c"/>
    <rect x="5" y="26.5" width="22" height="9" rx="1.5" fill="#3b3f5c"/>
    <circle cx="9" cy="9" r="1.4" fill="#51cf66"/><circle cx="13" cy="9" r="1.4" fill="#51cf66"/><path d="M17 9 H24" stroke="#262840" stroke-width="2" stroke-linecap="round"/>
    <circle cx="9" cy="20" r="1.4" fill="#51cf66"/><circle cx="13" cy="20" r="1.4" fill="#fab005"/><path d="M17 20 H24" stroke="#262840" stroke-width="2" stroke-linecap="round"/>
    <circle cx="9" cy="31" r="1.4" fill="#ff4d5e"/><circle cx="13" cy="31" r="1.4" fill="#51cf66"/><path d="M17 31 H24" stroke="#262840" stroke-width="2" stroke-linecap="round"/>`],
  telescope: ["0 0 48 40", `
    <path d="M22 22 L12 39 M24 22 V39 M26 22 L36 39" stroke="${OUT}" stroke-width="2" stroke-linecap="round"/>
    <rect x="9" y="9" width="32" height="9" rx="4.5" transform="rotate(-28 25 13.5)" fill="#14d1b5" ${THIN}/>
    <rect x="36" y="2" width="8" height="11" rx="2" transform="rotate(-28 40 7.5)" fill="#0e8f7f" ${THIN}/>
    <rect x="5" y="17" width="7" height="6" rx="2" transform="rotate(-28 8.5 20)" fill="#2b3350" ${THIN}/>
    <circle cx="24" cy="21" r="3" fill="#2b3350" ${THIN}/>`],
  sofa: ["0 0 64 32", `
    <rect x="5" y="2" width="54" height="15" rx="6" fill="#3d6fb0" ${THIN}/>
    <rect x="1" y="11" width="10" height="17" rx="4" fill="#3561a0" ${THIN}/>
    <rect x="53" y="11" width="10" height="17" rx="4" fill="#3561a0" ${THIN}/>
    <rect x="10" y="15" width="22" height="10" rx="3.5" fill="#4d84cc" ${THIN}/>
    <rect x="32" y="15" width="22" height="10" rx="3.5" fill="#4d84cc" ${THIN}/>
    <path d="M6 28 V31 M58 28 V31" stroke="${OUT}" stroke-width="2.4" stroke-linecap="round"/>`],
  easel: ["0 0 40 32", `
    <path d="M9 31 L15 14 M31 31 L25 14 M20 14 V31" stroke="${WOOD_DARK}" stroke-width="2.4" stroke-linecap="round"/>
    <rect x="4" y="1.5" width="32" height="18" rx="1.5" fill="#fff4fa" ${THIN}/>
    <circle cx="13" cy="9" r="4" fill="#ff4fa3" opacity=".85"/>
    <path d="M18 15 Q24 4 31 10" fill="none" stroke="#26e0ff" stroke-width="2.6" stroke-linecap="round"/>
    <circle cx="27" cy="14.5" r="2.4" fill="#ffd23f"/>
    <rect x="3" y="19" width="34" height="2.6" rx="1.2" fill="${WOOD}" ${THIN}/>`],
  board: ["0 0 64 28", `
    <rect x="1" y="1" width="62" height="26" rx="3" fill="#1b2036" ${THIN}/>
    <rect x="4" y="4" width="56" height="20" rx="1.5" fill="#141a33"/>
    <path d="M7 19 L15 13 L22 16 L31 8 L38 12" fill="none" stroke="#26e0ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <rect x="43" y="14" width="3.5" height="7" rx="1" fill="#8b5cff"/><rect x="48" y="10" width="3.5" height="11" rx="1" fill="#ff4fa3"/><rect x="53" y="7" width="3.5" height="14" rx="1" fill="#51cf66"/>
    <circle cx="9" cy="7.5" r="1.4" fill="#ff4d5e"/>`],
  table: ["0 0 64 24", `
    <path d="M30 13 V23 M24 23 H40" stroke="${OUT}" stroke-width="2.4" stroke-linecap="round"/>
    <ellipse cx="32" cy="9" rx="30" ry="7.5" fill="#5a2fd6" ${THIN}/>
    <ellipse cx="32" cy="7.5" rx="25" ry="4.5" fill="#7b4dff"/>
    <circle cx="22" cy="7" r="2" fill="#e4e8f4"/><rect x="38" y="5" width="7" height="4" rx="1" fill="#ffd23f"/>`],
  cooler: ["0 0 24 36", `
    <rect x="5" y="1" width="14" height="13" rx="5" fill="#a5d8ff" ${THIN}/>
    <path d="M8 4.5 V11" stroke="#e7f5ff" stroke-width="2" stroke-linecap="round"/>
    <rect x="3" y="14" width="18" height="21" rx="2.5" fill="#dee2e6" ${THIN}/>
    <rect x="7" y="18" width="10" height="5" rx="1.5" fill="#495057"/>
    <circle cx="9.5" cy="20.5" r="1.2" fill="#4dabf7"/><circle cx="14.5" cy="20.5" r="1.2" fill="#ff6b6b"/>`],
  cabinet: ["0 0 32 40", `
    <rect x="2" y="1" width="28" height="38" rx="2.5" fill="#6b7590" ${THIN}/>
    <rect x="5" y="4" width="22" height="9.5" rx="1.5" fill="#8790a8" ${THIN}/>
    <rect x="5" y="15.5" width="22" height="9.5" rx="1.5" fill="#8790a8" ${THIN}/>
    <rect x="5" y="27" width="22" height="9.5" rx="1.5" fill="#8790a8" ${THIN}/>
    <rect x="12" y="7.5" width="8" height="2.4" rx="1.2" fill="#ffd23f"/><rect x="12" y="19" width="8" height="2.4" rx="1.2" fill="#ffd23f"/><rect x="12" y="30.5" width="8" height="2.4" rx="1.2" fill="#ffd23f"/>`],
};

export function furnitureSvg(name) {
  const [viewBox, markup] = FURNITURE[name];
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `furniture f-${name}`);
  svg.innerHTML = markup; // constant markup from this file, never data
  return svg;
}
