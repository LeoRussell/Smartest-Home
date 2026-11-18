/* Small interface icons, drawn locally and labelled by their callers. */
(() => {
    "use strict";
    const paths = {
        list: ["M8 5h13M8 12h13M8 19h13", "M3 5h.01M3 12h.01M3 19h.01"],
        chart: ["M3 3v18h18", "m6 15 5-6 4 3 5-8"],
        download: ["M12 3v12m-5-5 5 5 5-5", "M4 17v4h16v-4"],
        upload: ["M12 16V4m-5 5 5-5 5 5", "M4 17v4h16v-4"],
        refresh: ["M20 7v5h-5", "M20 12a8 8 0 1 0-2 6M20 7l-3-3"],
        close: ["m6 6 12 12M18 6 6 18"],
        save: ["M4 3h13l4 4v14H3V3Z", "M7 3v6h10V3M7 21v-8h10v8"],
        settings: ["M10 3h4l1 3 3 1 3-1 2 4-2 2 1 3 1 2-3 3-3-1-2 1-1 3h-4l-1-3-3-1-3 1-2-4 2-2-1-3-1-2 3-3 3 1 2-1Z", "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"],
        history: ["M4 5v6h6", "M4 11a8 8 0 1 1 2 7M12 7v5l3 2"],
        info: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M12 10v7m0-11v1"],
        plus: ["M12 4v16M4 12h16"],
        play: ["m8 4 12 8-12 8Z"],
        check: ["m4 12 5 5L20 6"],
        code: ["m8 5-6 7 6 7m8-14 6 7-6 7M14 3l-4 18"],
        undo: ["M8 4 3 9l5 5", "M3 9h10a7 7 0 0 1 7 7v4"],
        trash: ["M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"]
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
        if (action) element.addEventListener("click", action);
        return element;
    }
    function labelAction(element, caption) {
        let copy = element.querySelector("[data-icon-label]");
        if (!copy) {
            for (const child of [...element.childNodes]) if (child.nodeType === Node.TEXT_NODE) child.remove();
            copy = document.createElement("span"); copy.dataset.iconLabel = ""; element.append(copy);
        }
        copy.textContent = caption;
        label(element, caption);
    }
    function action(name, caption, handler, className = "") {
        const element = document.createElement("button"); element.type = "button";
        element.className = `gateway-action-button ${className}`.trim(); element.append(create(name));
        labelAction(element, caption);
        if (handler) element.addEventListener("click", handler);
        return element;
    }
    window.GatewayIcons = { create, label, button, action, labelAction };
})();
