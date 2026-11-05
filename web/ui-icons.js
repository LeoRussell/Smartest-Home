/* Small interface icons, drawn locally and labelled by their callers. */
(() => {
    "use strict";
    const paths = {
        list: ["M8 5h13M8 12h13M8 19h13", "M3 5h.01M3 12h.01M3 19h.01"],
        chart: ["M3 3v18h18", "m6 15 5-6 4 3 5-8"],
        download: ["M12 3v12m-5-5 5 5 5-5", "M4 17v4h16v-4"]
    };
    function create(name) {
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.setAttribute("viewBox", "0 0 24 24");
        svg.setAttribute("aria-hidden", "true");
        svg.setAttribute("focusable", "false");
        for (const shape of paths[name] || []) {
            const path = document.createElementNS(svg.namespaceURI, "path");
            path.setAttribute("d", shape); svg.append(path);
        }
        return svg;
    }
    function label(element, caption) {
        element.setAttribute("aria-label", caption);
        element.title = caption;
    }
    function button(name, caption, action, className = "") {
        const element = document.createElement("button");
        element.type = "button";
        element.className = `gateway-icon-button ${className}`.trim();
        element.append(create(name)); label(element, caption);
        element.addEventListener("click", action);
        return element;
    }
    window.GatewayIcons = { create, label, button };
})();
