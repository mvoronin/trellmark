// Vendored Lucide symbols remain explicit SVG nodes, never parsed markup.
// Color and size come from .icon CSS through currentColor.
const SVG_NS = "http://www.w3.org/2000/svg";
export function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("class", `icon icon-${name}`);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVG_NS, "use");
    use.setAttribute("href", `/static/icons.svg#${name}`);
    svg.append(use);
    return svg;
}
