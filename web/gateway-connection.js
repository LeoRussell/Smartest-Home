/* Browser-local Gateway address. Changes take effect after a full reload. */
(() => {
    "use strict";
    const KEY = "iot-gateway-connection-v1";
    // The combined launcher supplies only a port. Use the page's host so a
    // phone never connects to its own localhost or the server's wildcard IP.
    const configuredPort = window.__GATEWAY_CONFIG__?.gateway_port;
    const defaultPort = Number.isInteger(configuredPort) && configuredPort >= 1 && configuredPort <= 65535 ? configuredPort : 8765;
    let options, ui, connected = false, feedback = null, discardedWildcardAddress = false;
    const t = (ru, en) => options.localized(ru, en);
    const node = (tag, ru, en, className) => {
        const element = document.createElement(tag);
        if (ru != null) { element.dataset.ru = ru; element.dataset.en = en; element.textContent = t(ru, en); }
        if (className) element.className = className;
        return element;
    };
    const isWildcardHost = hostname => ["0.0.0.0", "[::]"].includes(hostname);
    function normalize(value) {
        if (typeof value !== "string" || value.length > 2048) throw new Error("invalid_address");
        const url = new URL(value.trim());
        if (url.protocol === "http:") url.protocol = "ws:";
        if (url.protocol === "https:") url.protocol = "wss:";
        if (!["ws:", "wss:"].includes(url.protocol) || !url.hostname || url.port === "0" ||
            url.username || url.password || url.pathname !== "/" || url.href.includes("?") || url.href.includes("#")) throw new Error("invalid_address");
        // URL parsing also canonicalizes aliases such as 0, 0.0 and 0x0.
        if (isWildcardHost(url.hostname)) throw new Error("wildcard_address");
        if (window.location.protocol === "https:" && url.protocol !== "wss:") throw new Error("secure_address_required");
        return url.href;
    }
    function defaultUrl(port = defaultPort) {
        const pageHost = window.location.hostname;
        const host = pageHost === "[::]" ? "[::1]" : pageHost === "0.0.0.0" || !pageHost ? "localhost" : pageHost;
        return `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${host}:${port}/`;
    }
    function savedUrl() {
        try { return normalize(JSON.parse(localStorage.getItem(KEY))?.url); }
        catch (error) {
            if (error.message === "wildcard_address") {
                discardedWildcardAddress = true;
                try { localStorage.removeItem(KEY); } catch { /* The invalid address is still ignored. */ }
            }
            return "";
        }
    }
    // Another tab may edit shared storage while this page has a live socket.
    // Keep downloads and reconnects on that socket's Gateway until a reload.
    const activeUrl = savedUrl() || defaultUrl();
    function url() { return activeUrl; }
    function downloadUrl(path) {
        const base = new URL(url()); base.protocol = base.protocol === "wss:" ? "https:" : "http:";
        return new URL(path, base).href;
    }
    function showFeedback(ru, en, state, connectionError = false) {
        feedback = { ru, en, state, connectionError }; ui.feedback.textContent = t(ru, en); ui.feedback.dataset.state = state;
    }
    function apply(value) {
        let next;
        try { next = value.trim() ? normalize(value) : ""; }
        catch (error) {
            if (error.message === "wildcard_address") {
                showFeedback("0.0.0.0 и :: — адреса прослушивания сервера. Укажите IP компьютера с Gateway; на этом же компьютере можно использовать localhost.",
                    "0.0.0.0 and :: are server listen addresses. Enter the Gateway computer's IP; use localhost when the Gateway runs on this computer.", "error");
                return;
            }
            showFeedback(error.message === "secure_address_required"
                ? "Для страницы HTTPS используйте защищённый адрес wss://."
                : "Укажите адрес вида ws://192.168.0.102:8765 без пути, пароля и параметров.",
            error.message === "secure_address_required"
                ? "An HTTPS page requires a secure wss:// address."
                : "Enter an address such as ws://192.168.0.102:8765 without a path, password or query parameters.", "error");
            return;
        }
        const previous = url();
        try {
            if (next) localStorage.setItem(KEY, JSON.stringify({ url: next }));
            else localStorage.removeItem(KEY);
        } catch {
            showFeedback("Браузер не разрешил сохранить адрес. Разрешите локальное хранилище и повторите.", "The browser could not save the address. Allow local storage and try again.", "error");
            return;
        }
        if ((next || defaultUrl()) !== previous) {
            // Event IDs refer to one database, so discard that database's cache.
            try { localStorage.removeItem("iot-gateway-notifications-v1"); } catch { /* Optional cache. */ }
            options.reload();
        } else {
            ui.input.value = savedUrl();
            showFeedback("Сохранено. Адрес не изменился.", "Saved. The address is unchanged.", "success"); render();
        }
    }
    function render() {
        if (!ui) return;
        ui.input.placeholder = defaultUrl();
        ui.status.textContent = `${t("Адрес", "Address")}: ${url()} · ${connected ? t("На связи", "Online") : t("Нет связи", "Offline")}`;
        ui.status.dataset.state = connected ? "success" : "info";
        if (feedback) showFeedback(feedback.ru, feedback.en, feedback.state, feedback.connectionError);
    }
    window.GatewayConnection = {
        url, downloadUrl, normalize, isDefault: () => activeUrl === defaultUrl(),
        isLegacyDefault: () => activeUrl === defaultUrl(8765),
        initialize(context) {
            options = context;
            const section = node("section", null, null, "settings-group gateway-connection-settings");
            const heading = node("div", null, null, "settings-group-heading");
            const copy = document.createElement("div");
            copy.append(node("h2", "Подключение к Gateway", "Gateway connection"), node("p", "Адрес шлюза для этого браузера", "Gateway address for this browser"));
            const icon = node("span", "↔", "↔", "settings-group-icon"); icon.setAttribute("aria-hidden", "true"); heading.append(icon, copy);
            const form = node("form", null, null, "gateway-connection-form");
            const label = node("label", null, null, "management-field");
            const input = document.createElement("input"); input.type = "url"; input.id = "gateway-address";
            input.maxLength = 2048; input.autocomplete = "off"; input.spellcheck = false; input.value = savedUrl();
            label.append(node("span", "Адрес WebSocket", "WebSocket address"), input);
            const save = node("button", "Сохранить и подключиться", "Save and connect"); save.type = "submit";
            const reset = node("button", "Автоматически", "Use default"); reset.type = "button"; reset.addEventListener("click", () => apply(""));
            const actions = node("div", null, null, "gateway-connection-actions"); actions.append(save, reset);
            const status = node("p", null, null, "management-feedback gateway-connection-status");
            const message = node("p", null, null, "management-feedback"); message.setAttribute("role", "status");
            const help = node("p", `Пустое поле — адрес этой страницы, порт ${defaultPort}. При смене адреса страница перезагрузится; устройства и база остаются на выбранном Gateway.`,
                `Leave empty to use this page's host on port ${defaultPort}. Changing the address reloads this page; devices and data belong to the selected Gateway.`, "management-help");
            help.id = "gateway-address-help"; input.setAttribute("aria-describedby", help.id);
            form.append(label, actions); form.addEventListener("submit", event => { event.preventDefault(); apply(input.value); });
            section.append(heading, form, help, status, message); document.querySelector("#settings-page .settings-card").append(section);
            ui = { input, status, feedback: message };
            if (discardedWildcardAddress) showFeedback("Сохранённый адрес прослушивания удалён. Используется автоматический адрес; при необходимости укажите IP компьютера с Gateway.",
                "The saved server listen address was removed. Using the automatic address; enter the Gateway computer's IP if needed.", "warning");
            render();
            window.addEventListener("storage", event => {
                if (event.key !== KEY && event.key !== null || (savedUrl() || defaultUrl()) === url()) return;
                if (document.activeElement !== input) input.value = savedUrl();
                showFeedback("Адрес изменён в другой вкладке. Эта вкладка продолжает работать с прежним Gateway до перезагрузки страницы.",
                    "The address changed in another tab. This tab keeps using its original Gateway until the page is reloaded.", "warning");
            });
        },
        status(value) {
            connected = Boolean(value);
            if (connected && feedback?.connectionError) {
                feedback = null;
                if (ui) { ui.feedback.textContent = ""; delete ui.feedback.dataset.state; }
            }
            render();
        },
        unavailable() {
            if (feedback?.state === "error" && !feedback.connectionError) return;
            if (ui) showFeedback(`Нет ответа от Gateway по адресу ${url()}. Проверьте IP, порт и запуск Gateway на выбранном компьютере. «Автоматически» использует ${defaultUrl()}`,
                `Gateway did not respond at ${url()}. Check the IP, port and that Gateway is running on the selected computer. Use default connects to ${defaultUrl()}`, "error", true);
        },
        failed() {
            if (feedback?.state === "error" && !feedback.connectionError) return;
            if (ui) showFeedback("Браузер не разрешил подключение к этому адресу. Проверьте протокол и порт Gateway.",
                "The browser did not allow this connection. Check the Gateway protocol and port.", "error", true);
        },
        languageChanged: render
    };
})();
