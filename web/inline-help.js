/* Small contextual explanations. No Gateway messages or HTML from device text. */
(() => {
    "use strict";
    const entries = new WeakMap();
    let sequence = 0, active = null, hideTimer = null, frame = null, observer = null;
    const defaultLabel = () => document.documentElement.lang === "en" ? "Help" : "Пояснение";
    const owns = (entry, target) => target instanceof Node && entry.root.contains(target);

    function position() {
        frame = null;
        if (!active) return;
        const { root, trigger, panel } = active;
        if (!root.isConnected || root.closest("[hidden]") || !trigger.getClientRects().length) { close(); return; }
        const viewport = window.visualViewport;
        const leftEdge = viewport?.offsetLeft || 0, topEdge = viewport?.offsetTop || 0;
        const width = viewport?.width || document.documentElement.clientWidth;
        const height = viewport?.height || window.innerHeight;
        const margin = 8, gap = 6, anchor = trigger.getBoundingClientRect();
        if (anchor.bottom <= topEdge || anchor.top >= topEdge + height || anchor.right <= leftEdge || anchor.left >= leftEdge + width) { close(); return; }
        panel.style.maxWidth = `${Math.max(1, Math.min(360, width - margin * 2))}px`;
        panel.style.maxHeight = `${Math.max(1, height - margin * 2)}px`;
        let box = panel.getBoundingClientRect();
        const below = Math.max(0, topEdge + height - margin - anchor.bottom - gap);
        const above = Math.max(0, anchor.top - topEdge - margin - gap);
        const placeAbove = box.height > below && above > below;
        // Keep the trigger visible for a second tap, even when a long mobile
        // explanation needs its own scrollbar.
        panel.style.maxHeight = `${Math.max(1, placeAbove ? above : below)}px`;
        box = panel.getBoundingClientRect();
        const left = Math.min(Math.max(anchor.left, leftEdge + margin), Math.max(leftEdge + margin, leftEdge + width - box.width - margin));
        let top = placeAbove ? anchor.top - box.height - gap : anchor.bottom + gap;
        top = Math.min(Math.max(top, topEdge + margin), Math.max(topEdge + margin, topEdge + height - box.height - margin));
        panel.style.left = `${Math.round(left)}px`;
        panel.style.top = `${Math.round(top)}px`;
    }

    function reposition() {
        if (active && frame === null) frame = requestAnimationFrame(position);
    }

    function close() {
        clearTimeout(hideTimer); hideTimer = null;
        if (frame !== null) cancelAnimationFrame(frame);
        frame = null;
        observer?.disconnect(); observer = null;
        const entry = active; active = null;
        if (!entry) return;
        entry.pinned = false;
        entry.trigger.setAttribute("aria-expanded", "false");
        try { if (entry.panel.matches(":popover-open")) entry.panel.hidePopover(); } catch { /* Older browsers use the fixed fallback. */ }
        entry.panel.hidden = true;
        entry.root.removeAttribute("data-open");
    }

    function show(entry, pinned = false) {
        if (!entry?.text || !entry.root.isConnected) return;
        clearTimeout(hideTimer); hideTimer = null;
        if (active !== entry) close();
        active = entry; entry.pinned = entry.pinned || pinned;
        entry.panel.hidden = false;
        entry.trigger.setAttribute("aria-expanded", "true");
        entry.root.dataset.open = "true";
        if (typeof entry.panel.showPopover === "function") {
            try { if (!entry.panel.matches(":popover-open")) entry.panel.showPopover(); }
            catch { entry.panel.removeAttribute("popover"); }
        }
        position();
        if (!observer && active) {
            observer = new MutationObserver(reposition);
            observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "open"] });
        }
    }

    function scheduleHide(entry) {
        if (!entry || active !== entry || entry.pinned) return;
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => {
            hideTimer = null;
            if (active !== entry || entry.pinned || owns(entry, document.activeElement) || entry.root.matches(":hover") || entry.panel.matches(":hover")) return;
            close();
        }, 160);
    }

    function entryFor(target) {
        return target instanceof Element ? entries.get(target.closest(".gateway-help")) : null;
    }

    function update(root, text, label) {
        const entry = entries.get(root);
        if (!entry) return root;
        entry.text = typeof text === "string" ? text.trim() : "";
        entry.panel.textContent = entry.text;
        entry.trigger.setAttribute("aria-label", label || defaultLabel());
        root.hidden = !entry.text;
        if (active === entry) { if (!entry.text) close(); else reposition(); }
        return root;
    }

    function create(text, label) {
        const root = document.createElement("span"); root.className = "gateway-help";
        const trigger = document.createElement("button"); trigger.type = "button"; trigger.className = "gateway-help-trigger";
        const glyph = document.createElement("span"); glyph.textContent = "i"; glyph.setAttribute("aria-hidden", "true");
        trigger.append(glyph);
        const panel = document.createElement("span"); panel.className = "gateway-help-panel";
        panel.id = `gateway-help-${++sequence}`; panel.setAttribute("role", "tooltip"); panel.hidden = true;
        panel.setAttribute("popover", "manual");
        trigger.setAttribute("aria-controls", panel.id);
        trigger.setAttribute("aria-describedby", panel.id);
        trigger.setAttribute("aria-expanded", "false");
        root.append(trigger, panel);
        entries.set(root, { root, trigger, panel, pinned: false, text: "" });
        // A help button can sit beside a label's caption. Keep label activation
        // from toggling a checkbox or opening a select when help is requested.
        root.addEventListener("click", event => {
            event.preventDefault(); event.stopPropagation();
            if (!event.target.closest(".gateway-help-trigger")) return;
            const entry = entries.get(root);
            if (active === entry && entry.pinned) close(); else show(entry, true);
        });
        return update(root, text, label);
    }

    document.addEventListener("pointerover", event => {
        if (event.pointerType === "touch") return;
        const entry = entryFor(event.target);
        if (entry && !owns(entry, event.relatedTarget)) show(entry);
    });
    document.addEventListener("pointerout", event => {
        const entry = entryFor(event.target);
        if (entry && !owns(entry, event.relatedTarget)) scheduleHide(entry);
    });
    document.addEventListener("focusin", event => {
        const entry = entryFor(event.target);
        if (entry && event.target === entry.trigger) show(entry);
        else if (active && !owns(active, event.target)) close();
    });
    document.addEventListener("focusout", event => {
        const entry = entryFor(event.target);
        if (entry && !owns(entry, event.relatedTarget)) scheduleHide(entry);
    });
    document.addEventListener("pointerdown", event => { if (active && !owns(active, event.target)) close(); }, true);
    document.addEventListener("keydown", event => {
        if (event.key !== "Escape" || !active) return;
        event.preventDefault(); event.stopImmediatePropagation(); close();
    }, true);
    document.addEventListener("close", event => { if (active && event.target instanceof Element && event.target.contains(active.root)) close(); }, true);
    document.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    window.visualViewport?.addEventListener("resize", reposition);
    window.visualViewport?.addEventListener("scroll", reposition);
    window.GatewayHelp = { create, update, closeAll: close };
})();
