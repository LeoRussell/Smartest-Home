(function () {
    "use strict";

    let workspace = null;
    let Blockly = null;
    let devices = [];
    let language = "ru";
    let container = null;
    let stateBeforeDeviceRefresh = null;
    const pendingDeviceIdRenames = new Map();

    const words = {
        ru: {
            events: "События", conditions: "Условия", logic: "Логика", actions: "Действия", values: "Значения",
            parameterChanged: "Когда изменилось значение", triggerFilter: "Фильтр", anyChange: "любое изменение", changedTo: "стало равно", invalidTriggerValue: "Задайте корректное значение фильтра запуска.", stableFor: "Без новых изменений", cooldown: "Пауза после запуска", seconds: "сек.", deviceConnected: "Когда устройство подключилось", deviceDisconnected: "Когда устройство отключилось", gatewayStarted: "При запуске Gateway",
            readParameter: "текущее значение параметра", readParameterHelp: "Подключите к сравнению или вычислению; сам по себе блок не запускает сценарий.", parameterValue: "значение параметра", equals: "равно", notEquals: "не равно", lessThan: "меньше", atMost: "не больше", greaterThan: "больше", atLeast: "не меньше",
            isConnected: "устройство подключено", setParameter: "установить параметр", invokeAction: "запустить действие / режим",
            choose: "выберите устройство", parameter: "параметр", action: "действие", mode: "режим", operation: "операция", selectMode: "выполнить режим", enableMode: "включить режим", disableMode: "выключить режим", and: "И", or: "ИЛИ", noParameters: "нет доступных параметров", noActions: "нет объявленных действий", noModes: "нет зарегистрированных режимов",
            notSet: "(не задано)", off: "Выкл.", on: "Вкл.",
            wait: "подождать", seconds: "секунд",
            invalidWorkspace: "Добавьте один блок запуска и соедините с ним сценарий.", emptyBody: "В сценарии нет действий.",
            missingBlock: "Сценарий содержит неподдерживаемый или неполный блок.", invalidArgument: "Проверьте значение аргумента действия."
        },
        en: {
            events: "Events", conditions: "Conditions", logic: "Logic", actions: "Actions", values: "Values",
            parameterChanged: "When a parameter changes", triggerFilter: "Filter", anyChange: "any change", changedTo: "changed to", invalidTriggerValue: "Enter a valid trigger filter value.", stableFor: "No further changes for", cooldown: "Cooldown", seconds: "sec.", deviceConnected: "When a device connects", deviceDisconnected: "When a device disconnects", gatewayStarted: "When Gateway starts",
            readParameter: "current parameter value", readParameterHelp: "Connect this to a comparison or calculation; it does not start a scenario by itself.", parameterValue: "parameter value", equals: "equals", notEquals: "does not equal", lessThan: "is less than", atMost: "is at most", greaterThan: "is greater than", atLeast: "is at least",
            isConnected: "device is connected", setParameter: "set parameter", invokeAction: "run device action / mode",
            choose: "select a device", parameter: "parameter", action: "action", mode: "mode", operation: "operation", selectMode: "run mode", enableMode: "enable mode", disableMode: "disable mode", and: "AND", or: "OR", noParameters: "no writable parameters", noActions: "no declared actions", noModes: "no registered modes",
            notSet: "(not set)", off: "Off", on: "On",
            wait: "wait", seconds: "seconds",
            invalidWorkspace: "Add one trigger block and connect the scenario to it.", emptyBody: "The scenario has no actions.",
            missingBlock: "The scenario contains an unsupported or incomplete block.", invalidArgument: "Check the action argument value."
        }
    };

    function text(key) {
        return words[language][key] || key;
    }

    function deviceOptions() {
        if (!devices.length) return [[text("choose"), ""]];
        return devices.map(device => [device.device, device.device]);
    }

    function applyDeviceIdRenames(state) {
        if (!state || !pendingDeviceIdRenames.size) return state;
        const visit = value => {
            if (Array.isArray(value)) {
                value.forEach(visit);
                return;
            }
            if (!value || typeof value !== "object") return;
            const fields = value.fields;
            if (fields && typeof fields === "object" && typeof fields.DEVICE === "string") {
                fields.DEVICE = pendingDeviceIdRenames.get(fields.DEVICE) || fields.DEVICE;
            }
            if (value.name === "DEVICE" && typeof value.value === "string") {
                value.value = pendingDeviceIdRenames.get(value.value) || value.value;
            }
            Object.values(value).forEach(visit);
        };
        visit(state);
        pendingDeviceIdRenames.clear();
        return state;
    }

    function parameterOptions() {
        const block = this.getSourceBlock();
        const device = devices.find(item => item.device === block?.getFieldValue("DEVICE"));
        const parameters = device?.schema?.parameters || {};
        const options = Object.entries(parameters)
            .filter(([, definition]) => definition.type !== "json" && definition.access !== "write_only")
            .map(([id, definition]) => [definition.label || id, id]);
        return options.length ? options : [[text("noParameters"), ""]];
    }

    function writableParameterOptions() {
        const block = this.getSourceBlock();
        const device = devices.find(item => item.device === block?.getFieldValue("DEVICE"));
        const parameters = device?.schema?.parameters || {};
        const options = Object.entries(parameters)
            .filter(([, definition]) => definition.access === "read_write")
            .map(([id, definition]) => [definition.label || id, id]);
        return options.length ? options : [[text("noParameters"), ""]];
    }

    function actionOptions() {
        const block = this.getSourceBlock();
        const device = devices.find(item => item.device === block?.getFieldValue("DEVICE"));
        const actions = device?.schema?.actions || {};
        const modeActionIds = new Set(Object.values(device?.schema?.modes || {})
            .flatMap(mode => Object.values(mode.operations || {})));
        const options = Object.entries(actions)
            .filter(([id]) => !modeActionIds.has(id))
            .filter(([, definition]) => Object.values(definition.arguments || {}).every(argument =>
                argument.type !== "json" || argument.required === false
            ))
            .map(([id, definition]) => [definition.label || id, id]);
        return options.length ? options : [[text("noActions"), ""]];
    }

    function modeOptions() {
        const block = this.getSourceBlock();
        const device = devices.find(item => item.device === block?.getFieldValue("DEVICE"));
        const modes = device?.schema?.modes || {};
        const options = Object.entries(modes)
            .filter(([, definition]) => Object.keys(definition.operations || {}).length > 0)
            .map(([id, definition]) => [definition.label || id, id]);
        return options.length ? options : [[text("noModes"), ""]];
    }

    function modeOperationOptions() {
        const block = this.getSourceBlock();
        const device = devices.find(item => item.device === block?.getFieldValue("DEVICE"));
        const mode = device?.schema?.modes?.[block?.getFieldValue("MODE")];
        const labels = {
            select: text("selectMode"), enable: text("enableMode"), disable: text("disableMode")
        };
        const options = Object.keys(mode?.operations || {})
            .map(operation => [labels[operation] || operation, operation]);
        return options.length ? options : [[text("operation"), ""]];
    }

    function modeActionForBlock(block) {
        const device = devices.find(item => item.device === block.getFieldValue("DEVICE"));
        const mode = device?.schema?.modes?.[block.getFieldValue("MODE")];
        return mode?.operations?.[block.getFieldValue("OPERATION")] || "";
    }

    function selectedParameter(block, fieldName = "PARAMETER") {
        if (!block) return null;
        const device = devices.find(item => item.device === block.getFieldValue("DEVICE"));
        return device?.schema?.parameters?.[block.getFieldValue(fieldName)] || null;
    }

    function triggerValueField(definition) {
        if (definition?.type === "boolean") {
            return new Blockly.FieldDropdown([
                [text("on"), "true"], [text("off"), "false"]
            ]);
        }
        if (definition?.type === "enum") {
            const options = (definition.options || []).map(option => [
                option.label, JSON.stringify(option.value)
            ]);
            if (options.length) return new Blockly.FieldDropdown(options);
        }
        if (definition?.type === "integer" || definition?.type === "number") {
            const initial = definition.min ?? 0;
            const precision = definition.type === "integer"
                ? 1
                : definition.step ?? (definition.precision != null
                    ? 10 ** -definition.precision : 0.01);
            return new Blockly.FieldNumber(
                initial,
                definition.min ?? -Infinity,
                definition.max ?? Infinity,
                precision
            );
        }
        return new Blockly.FieldTextInput("");
    }

    function updateParameterTriggerFilter(block) {
        const valueInput = block.getInput("FILTER_VALUE");
        if (!valueInput) return;
        const definition = selectedParameter(block);
        const nextType = definition?.type || "string";
        const schemaKey = `${block.getFieldValue("DEVICE")}:${block.getFieldValue("PARAMETER")}:${nextType}`;
        if (block.triggerValueSchemaKey_ !== schemaKey) {
            const oldType = block.triggerValueType_;
            const oldValue = block.getFieldValue("VALUE");
            valueInput.removeField("VALUE", true);
            valueInput.appendField(triggerValueField(definition), "VALUE");
            block.triggerValueType_ = nextType;
            block.triggerValueSchemaKey_ = schemaKey;

            // Keep the selected value across devices exposing the same type.
            if (oldType === nextType && oldValue !== null && oldValue !== undefined) {
                const field = block.getField("VALUE");
                const options = field.getOptions ? field.getOptions(false) : null;
                if (!options || options.some(option => option[1] === oldValue)) {
                    try { block.setFieldValue(oldValue, "VALUE"); } catch (error) { /* use the field default */ }
                }
            }
        }
        valueInput.setVisible(block.getFieldValue("MATCH") === "equals");
    }

    function parseTriggerValue(raw, definition) {
        if (!definition) throw new Error(text("missingBlock"));
        if (definition.type === "boolean") return raw === "true";
        if (definition.type === "enum") {
            try { return JSON.parse(raw); } catch (error) { throw new Error(text("invalidTriggerValue")); }
        }
        if (definition.type === "integer") {
            const value = Number(raw);
            if (!Number.isSafeInteger(value)) throw new Error(text("invalidTriggerValue"));
            return value;
        }
        if (definition.type === "number") {
            const value = Number(raw);
            if (!Number.isFinite(value)) throw new Error(text("invalidTriggerValue"));
            return value;
        }
        if (definition.type === "string") return raw;
        throw new Error(text("invalidTriggerValue"));
    }

    function parameterComparisonOptions() {
        const definition = selectedParameter(this.getSourceBlock());
        const options = [[text("equals"), "eq"], [text("notEquals"), "ne"]];
        if (["integer", "number"].includes(definition?.type)) {
            options.push(
                [text("lessThan"), "lt"], [text("atMost"), "le"],
                [text("greaterThan"), "gt"], [text("atLeast"), "ge"]
            );
        }
        return options;
    }

    function updateParameterComparison(block) {
        const parameterChoices = parameterOptions.call(block.getField("PARAMETER"));
        if (!parameterChoices.some(option => option[1] === block.getFieldValue("PARAMETER"))) {
            block.setFieldValue(parameterChoices[0][1], "PARAMETER");
        }
        const operatorChoices = parameterComparisonOptions.call(block.getField("OPERATOR"));
        if (!operatorChoices.some(option => option[1] === block.getFieldValue("OPERATOR"))) {
            block.setFieldValue(operatorChoices[0][1], "OPERATOR");
        }
        updateValueConnection(block, "PARAMETER", "RIGHT");
    }

    function updateValueConnection(block, fieldName = "PARAMETER", inputName = null) {
        const definition = selectedParameter(block, fieldName);
        const check = definition?.type === "integer" || definition?.type === "number"
            ? "Number"
            : definition?.type === "boolean" ? "Boolean"
                : definition?.type === "string" ? "String" : null;
        if (inputName) block.getInput(inputName)?.setCheck(check);
        else block.outputConnection?.setCheck(check);
    }

    function makeArgumentField(argument, initialValue) {
        const optional = argument.required === false;
        if (argument.type === "boolean") {
            const options = optional ? [[text("notSet"), ""]] : [];
            options.push([text("off"), "false"], [text("on"), "true"]);
            return new Blockly.FieldDropdown(options);
        }
        if (argument.type === "enum") {
            const options = optional ? [[text("notSet"), ""]] : [];
            for (const item of argument.options || []) {
                options.push([item.label, JSON.stringify(item.value)]);
            }
            return new Blockly.FieldDropdown(options);
        }
        return new Blockly.FieldTextInput(initialValue ?? "");
    }

    function updateActionArguments(block, actionIdOverride = null) {
        if (block.getInput("ARGUMENTS")) block.removeInput("ARGUMENTS");
        const device = devices.find(item => item.device === block.getFieldValue("DEVICE"));
        const definition = device?.schema?.actions?.[actionIdOverride || block.getFieldValue("ACTION")];
        const argumentsMap = definition?.arguments || {};
        const input = block.appendDummyInput("ARGUMENTS");
        let fieldCount = 0;
        for (const [id, argument] of Object.entries(argumentsMap)) {
            if (argument.type === "json") continue;
            if (fieldCount > 0) input.appendField("·");
            input.appendField(argument.label || id);
            input.appendField(makeArgumentField(argument), `ARG_${id}`);
            fieldCount += 1;
        }
        if (!fieldCount) input.appendField(text("action"));
    }

    function defineBlocks() {
        const Blockly = window.Blockly;
        Blockly.Blocks.iot_when_parameter_changes = {
            init() {
                this.appendDummyInput()
                    .appendField(text("parameterChanged"))
                    .appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE")
                    .appendField(new Blockly.FieldDropdown(parameterOptions), "PARAMETER");
                this.appendDummyInput("FILTER")
                    .appendField(text("triggerFilter"))
                    .appendField(new Blockly.FieldDropdown([
                        [text("anyChange"), "any"], [text("changedTo"), "equals"]
                    ]), "MATCH");
                this.appendDummyInput("FILTER_VALUE")
                    .appendField(triggerValueField(selectedParameter(this)), "VALUE");
                this.appendDummyInput("STABILITY")
                    .appendField(text("stableFor"))
                    .appendField(new Blockly.FieldNumber(0, 0, 3600, 1), "FOR_SECONDS")
                    .appendField(text("seconds"));
                this.appendDummyInput("COOLDOWN")
                    .appendField(text("cooldown"))
                    .appendField(new Blockly.FieldNumber(0, 0, 86400, 1), "COOLDOWN_SECONDS")
                    .appendField(text("seconds"));
                this.triggerValueType_ = selectedParameter(this)?.type || "string";
                this.setNextStatement(true);
                this.setColour(210);
                this.setTooltip(text("parameterChanged"));
                updateParameterTriggerFilter(this);
            },
            onchange(event) {
                if (event.type !== Blockly.Events.BLOCK_CHANGE || event.blockId !== this.id) return;
                if (event.name === "DEVICE") {
                    const options = parameterOptions.call(this.getField("PARAMETER"));
                    if (!options.some(item => item[1] === this.getFieldValue("PARAMETER"))) {
                        this.setFieldValue(options[0][1], "PARAMETER");
                    }
                }
                if (["DEVICE", "PARAMETER", "MATCH"].includes(event.name)) {
                    updateParameterTriggerFilter(this);
                }
            }
        };
        Blockly.Blocks.iot_when_device_connects = {
            init() {
                this.appendDummyInput().appendField(text("deviceConnected")).appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE");
                this.setNextStatement(true);
                this.setColour(210);
            }
        };
        Blockly.Blocks.iot_when_device_disconnects = {
            init() {
                this.appendDummyInput().appendField(text("deviceDisconnected")).appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE");
                this.setNextStatement(true);
                this.setColour(210);
            }
        };
        Blockly.Blocks.iot_when_gateway_starts = {
            init() {
                this.appendDummyInput().appendField(text("gatewayStarted"));
                this.setNextStatement(true);
                this.setColour(210);
            }
        };
        Blockly.Blocks.iot_device_parameter = {
            init() {
                this.appendDummyInput()
                    .appendField(text("readParameter"))
                    .appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE")
                    .appendField(new Blockly.FieldDropdown(parameterOptions), "PARAMETER");
                this.setOutput(true);
                this.setColour(195);
                this.setTooltip(text("readParameterHelp"));
            },
            onchange(event) {
                if (event.type === Blockly.Events.BLOCK_CHANGE && event.blockId === this.id) {
                    if (event.name === "DEVICE") {
                        const options = parameterOptions.call(this.getField("PARAMETER"));
                        if (!options.some(item => item[1] === this.getFieldValue("PARAMETER"))) {
                            this.setFieldValue(options[0][1], "PARAMETER");
                        }
                    }
                    updateValueConnection(this);
                }
            }
        };
        Blockly.Blocks.iot_parameter_compare = {
            init() {
                this.appendDummyInput()
                    .appendField(text("parameterValue"))
                    .appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE")
                    .appendField(new Blockly.FieldDropdown(parameterOptions), "PARAMETER")
                    .appendField(new Blockly.FieldDropdown(parameterComparisonOptions), "OPERATOR");
                this.appendValueInput("RIGHT");
                this.setOutput(true, "Boolean");
                this.setColour(195);
                updateParameterComparison(this);
            },
            onchange(event) {
                if (event.type === Blockly.Events.BLOCK_CHANGE && event.blockId === this.id && ["DEVICE", "PARAMETER"].includes(event.name)) {
                    updateParameterComparison(this);
                }
            }
        };
        Blockly.Blocks.iot_device_connected = {
            init() {
                this.appendDummyInput().appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE").appendField(text("isConnected"));
                this.setOutput(true, "Boolean");
                this.setColour(195);
            }
        };
        Blockly.Blocks.iot_set_parameter = {
            init() {
                this.appendDummyInput()
                    .appendField(text("setParameter"))
                    .appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE")
                    .appendField(new Blockly.FieldDropdown(writableParameterOptions), "PARAMETER");
                this.appendValueInput("VALUE").appendField("=");
                this.setPreviousStatement(true);
                this.setNextStatement(true);
                this.setColour(25);
                updateValueConnection(this, "PARAMETER", "VALUE");
            },
            onchange(event) {
                if (event.type === Blockly.Events.BLOCK_CHANGE && event.blockId === this.id) {
                    if (event.name === "DEVICE") {
                        const options = writableParameterOptions.call(this.getField("PARAMETER"));
                        if (!options.some(item => item[1] === this.getFieldValue("PARAMETER"))) {
                            this.setFieldValue(options[0][1], "PARAMETER");
                        }
                    }
                    updateValueConnection(this, "PARAMETER", "VALUE");
                }
            }
        };
        Blockly.Blocks.iot_invoke_action = {
            init() {
                this.appendDummyInput("HEADER")
                    .appendField(text("invokeAction"))
                    .appendField(new Blockly.FieldDropdown(deviceOptions, function (value) {
                        const source = this.getSourceBlock();
                        const options = actionOptions.call(source.getField("ACTION"));
                        if (!options.some(item => item[1] === source.getFieldValue("ACTION"))) source.setFieldValue(options[0][1], "ACTION");
                        return value;
                    }), "DEVICE")
                    .appendField(new Blockly.FieldDropdown(actionOptions), "ACTION");
                this.setPreviousStatement(true);
                this.setNextStatement(true);
                this.setColour(25);
                updateActionArguments(this);
            },
            onchange(event) {
                if (event.type === Blockly.Events.BLOCK_CHANGE && event.blockId === this.id && ["DEVICE", "ACTION"].includes(event.name)) {
                    const options = actionOptions.call(this.getField("ACTION"));
                    if (!options.some(item => item[1] === this.getFieldValue("ACTION"))) this.setFieldValue(options[0][1], "ACTION");
                    updateActionArguments(this);
                }
            }
        };
        Blockly.Blocks.iot_invoke_mode = {
            init() {
                this.appendDummyInput("HEADER")
                    .appendField(text("selectMode"))
                    .appendField(new Blockly.FieldDropdown(deviceOptions), "DEVICE")
                    .appendField(new Blockly.FieldDropdown(modeOptions), "MODE")
                    .appendField(new Blockly.FieldDropdown(modeOperationOptions), "OPERATION");
                this.setPreviousStatement(true);
                this.setNextStatement(true);
                this.setColour(25);
                updateActionArguments(this, modeActionForBlock(this));
            },
            onchange(event) {
                if (event.type !== Blockly.Events.BLOCK_CHANGE || event.blockId !== this.id) return;
                if (["DEVICE", "MODE"].includes(event.name)) {
                    const modeChoices = modeOptions.call(this.getField("MODE"));
                    if (!modeChoices.some(option => option[1] === this.getFieldValue("MODE"))) {
                        this.setFieldValue(modeChoices[0][1], "MODE");
                    }
                    const operationChoices = modeOperationOptions.call(this.getField("OPERATION"));
                    if (!operationChoices.some(option => option[1] === this.getFieldValue("OPERATION"))) {
                        this.setFieldValue(operationChoices[0][1], "OPERATION");
                    }
                }
                if (["DEVICE", "MODE", "OPERATION"].includes(event.name)) {
                    updateActionArguments(this, modeActionForBlock(this));
                }
            }
        };
        Blockly.Blocks.iot_wait_seconds = {
            init() {
                this.appendDummyInput()
                    .appendField(text("wait"))
                    .appendField(new Blockly.FieldNumber(1, 0, 3600, 1), "SECONDS")
                    .appendField(text("seconds"));
                this.setPreviousStatement(true);
                this.setNextStatement(true);
                this.setColour(25);
            }
        };
    }

    function toolbox() {
        return {
            kind: "categoryToolbox",
            contents: [
                { kind: "category", name: text("events"), colour: "210", contents: [
                    { kind: "block", type: "iot_when_parameter_changes" },
                    { kind: "block", type: "iot_when_device_connects" },
                    { kind: "block", type: "iot_when_device_disconnects" },
                    { kind: "block", type: "iot_when_gateway_starts" }
                ] },
                { kind: "category", name: text("conditions"), colour: "195", contents: [
                    { kind: "block", type: "controls_if" },
                    { kind: "block", type: "iot_parameter_compare" },
                    { kind: "block", type: "iot_device_connected" },
                    { kind: "block", type: "logic_compare" },
                    { kind: "block", type: "logic_operation" },
                    { kind: "block", type: "logic_negate" }
                ] },
                { kind: "category", name: text("actions"), colour: "25", contents: [
                    { kind: "block", type: "iot_set_parameter" },
                    { kind: "block", type: "iot_invoke_action" },
                    { kind: "block", type: "iot_invoke_mode" },
                    { kind: "block", type: "iot_wait_seconds" }
                ] },
                { kind: "category", name: text("values"), colour: "230", contents: [
                    { kind: "block", type: "iot_device_parameter" },
                    { kind: "block", type: "math_number" },
                    { kind: "block", type: "math_arithmetic" },
                    { kind: "block", type: "logic_boolean" },
                    { kind: "block", type: "text" }
                ] }
            ]
        };
    }

    function disposeWorkspace() {
        if (workspace) {
            workspace.dispose();
            workspace = null;
        }
    }

    function refreshParameterBlockFields() {
        if (!workspace) return;
        Blockly.Events.disable();
        try {
            for (const block of workspace.getAllBlocks(false)) {
                if (["iot_when_parameter_changes", "iot_device_parameter"].includes(block.type)) {
                    const options = parameterOptions.call(block.getField("PARAMETER"));
                    if (block.getFieldValue("PARAMETER") === "" && options[0]?.[1]) {
                        block.setFieldValue(options[0][1], "PARAMETER");
                    }
                    if (block.type === "iot_device_parameter") updateValueConnection(block);
                    else updateParameterTriggerFilter(block);
                } else if (block.type === "iot_parameter_compare") {
                    updateParameterComparison(block);
                }
            }
        } finally {
            Blockly.Events.enable();
        }
    }

    function initialize(containerId, nextDevices, nextLanguage, serializedState) {
        Blockly = window.Blockly;
        container = document.getElementById(containerId);
        if (!Blockly || !container) return false;
        devices = Array.isArray(nextDevices) ? nextDevices : [];
        language = nextLanguage === "en" ? "en" : "ru";
        applyBlocklyLocale();
        disposeWorkspace();
        defineBlocks();
        workspace = Blockly.inject(container, {
            toolbox: toolbox(),
            media: "vendor/blockly/media/",
            trashcan: true,
            sounds: false,
            move: { scrollbars: true, drag: true, wheel: true },
            zoom: { controls: true, wheel: true, startScale: 0.9, maxScale: 1.5, minScale: 0.55, scaleSpeed: 1.1 },
            grid: { spacing: 20, length: 2, colour: "#a8a49a", snap: false },
            renderer: "geras"
        });
        workspace.addChangeListener(announceHistoryState);
        serializedState = applyDeviceIdRenames(serializedState);
        if (serializedState?.blocks) {
            try {
                Blockly.serialization.workspaces.load(serializedState, workspace);
            } catch (error) {
                console.warn("Could not restore automation workspace:", error);
            }
        }
        refreshParameterBlockFields();
        requestAnimationFrame(() => Blockly.svgResize(workspace));
        announceHistoryState();
        return true;
    }

    function announceHistoryState() {
        if (!workspace) return;
        const undoStack = workspace.getUndoStack?.() || workspace.undoStack_ || [];
        const redoStack = workspace.getRedoStack?.() || workspace.redoStack_ || [];
        document.dispatchEvent(new CustomEvent("automation-history-change", {
            detail: {
                canUndo: undoStack.length > 0,
                canRedo: redoStack.length > 0
            }
        }));
    }

    function applyBlocklyLocale() {
        const messages = window.BlocklyLocales?.[language];
        if (!messages || !window.Blockly?.Msg) return;
        const target = Blockly.Msg;
        for (const key of Object.keys(target)) delete target[key];
        Object.assign(target, messages);
    }

    function parseValue(block, depth = 0) {
        if (!block || depth > 20) throw new Error(text("missingBlock"));
        switch (block.type) {
            case "math_number": {
                const value = Number(block.getFieldValue("NUM"));
                if (!Number.isFinite(value)) throw new Error(text("missingBlock"));
                return { type: "constant", value };
            }
            case "math_arithmetic": {
                const operators = { ADD: "add", MINUS: "subtract", MULTIPLY: "multiply", DIVIDE: "divide", POWER: "power" };
                const operator = operators[block.getFieldValue("OP")];
                if (!operator) throw new Error(text("missingBlock"));
                return {
                    type: "arithmetic", operator,
                    left: parseValue(block.getInputTargetBlock("A"), depth + 1),
                    right: parseValue(block.getInputTargetBlock("B"), depth + 1)
                };
            }
            case "text": return { type: "constant", value: block.getFieldValue("TEXT") };
            case "logic_boolean": return { type: "constant", value: block.getFieldValue("BOOL") === "TRUE" };
            case "iot_device_parameter": return {
                type: "parameter", device: block.getFieldValue("DEVICE"), parameter: block.getFieldValue("PARAMETER")
            };
            case "iot_parameter_compare": return {
                type: "compare",
                operator: block.getFieldValue("OPERATOR"),
                left: {
                    type: "parameter", device: block.getFieldValue("DEVICE"), parameter: block.getFieldValue("PARAMETER")
                },
                right: parseValue(block.getInputTargetBlock("RIGHT"), depth + 1)
            };
            case "iot_device_connected": return { type: "device_connected", device: block.getFieldValue("DEVICE") };
            case "logic_negate": return {
                type: "not", operand: parseValue(block.getInputTargetBlock("BOOL"), depth + 1)
            };
            case "logic_operation": {
                const inputs = block.inputList
                    .filter(input => /^(A|B|ADD\d+)$/.test(input.name) && input.connection?.targetBlock())
                    .map(input => parseValue(input.connection.targetBlock(), depth + 1));
                if (inputs.length < 2) throw new Error(text("missingBlock"));
                return { type: block.getFieldValue("OP") === "AND" ? "and" : "or", operands: inputs };
            }
            case "logic_compare": {
                const operators = { EQ: "eq", NEQ: "ne", LT: "lt", LTE: "le", GT: "gt", GTE: "ge" };
                const operator = operators[block.getFieldValue("OP")];
                const left = parseValue(block.getInputTargetBlock("A"), depth + 1);
                const right = parseValue(block.getInputTargetBlock("B"), depth + 1);
                return { type: "compare", operator, left, right };
            }
            default: throw new Error(text("missingBlock"));
        }
    }

    function parseStatements(firstBlock, depth = 0) {
        if (depth > 20) throw new Error(text("missingBlock"));
        const statements = [];
        let block = firstBlock;
        let count = 0;
        while (block) {
            count += 1;
            if (count > 100) throw new Error(text("missingBlock"));
            if (block.type === "iot_set_parameter") {
                statements.push({
                    type: "set_parameter", device: block.getFieldValue("DEVICE"),
                    parameter: block.getFieldValue("PARAMETER"),
                    value: parseValue(block.getInputTargetBlock("VALUE"), depth + 1)
                });
            } else if (block.type === "iot_invoke_action") {
                const device = devices.find(item => item.device === block.getFieldValue("DEVICE"));
                const actionId = block.getFieldValue("ACTION");
                const definition = device?.schema?.actions?.[actionId];
                const args = {};
                for (const [id, argument] of Object.entries(definition?.arguments || {})) {
                    if (argument.type === "json") continue;
                    const raw = block.getFieldValue(`ARG_${id}`);
                    if (raw.trim() === "" && argument.required === false) continue;
                    if (raw.trim() === "" && argument.required !== false && ["integer", "number"].includes(argument.type)) {
                        throw new Error(text("invalidArgument"));
                    }
                    if (argument.type === "boolean") args[id] = raw === "true";
                    else if (argument.type === "enum") {
                        try { args[id] = JSON.parse(raw); } catch (error) { throw new Error(text("invalidArgument")); }
                    } else if (argument.type === "integer") {
                        const value = Number(raw);
                        if (!Number.isSafeInteger(value)) throw new Error(text("invalidArgument"));
                        args[id] = value;
                    } else if (argument.type === "number") {
                        const value = Number(raw);
                        if (!Number.isFinite(value)) throw new Error(text("invalidArgument"));
                        args[id] = value;
                    } else args[id] = raw;
                }
                statements.push({ type: "invoke_action", device: block.getFieldValue("DEVICE"), action: actionId, arguments: args });
            } else if (block.type === "iot_invoke_mode") {
                const deviceId = block.getFieldValue("DEVICE");
                const actionId = modeActionForBlock(block);
                const device = devices.find(item => item.device === deviceId);
                const definition = device?.schema?.actions?.[actionId];
                if (!definition) throw new Error(text("missingBlock"));
                const args = {};
                for (const [id, argument] of Object.entries(definition.arguments || {})) {
                    if (argument.type === "json") continue;
                    const raw = block.getFieldValue(`ARG_${id}`);
                    if (raw.trim() === "" && argument.required === false) continue;
                    if (raw.trim() === "" && argument.required !== false && ["integer", "number"].includes(argument.type)) {
                        throw new Error(text("invalidArgument"));
                    }
                    if (argument.type === "boolean") args[id] = raw === "true";
                    else if (argument.type === "enum") {
                        try { args[id] = JSON.parse(raw); } catch (error) { throw new Error(text("invalidArgument")); }
                    } else if (argument.type === "integer") {
                        const value = Number(raw);
                        if (!Number.isSafeInteger(value)) throw new Error(text("invalidArgument"));
                        args[id] = value;
                    } else if (argument.type === "number") {
                        const value = Number(raw);
                        if (!Number.isFinite(value)) throw new Error(text("invalidArgument"));
                        args[id] = value;
                    } else args[id] = raw;
                }
                statements.push({ type: "invoke_action", device: deviceId, action: actionId, arguments: args });
            } else if (block.type === "iot_wait_seconds") {
                const seconds = Number(block.getFieldValue("SECONDS"));
                if (!Number.isFinite(seconds) || seconds < 0 || seconds > 3600) {
                    throw new Error(text("missingBlock"));
                }
                statements.push({ type: "wait", seconds });
            } else if (block.type === "controls_if") {
                const branches = [];
                for (let index = 0; block.getInput(`IF${index}`); index += 1) {
                    branches.push({
                        condition: parseValue(block.getInputTargetBlock(`IF${index}`), depth + 1),
                        then: parseStatements(block.getInputTargetBlock(`DO${index}`), depth + 1)
                    });
                }
                const elseBlock = block.getInputTargetBlock("ELSE");
                statements.push({ type: "if", branches, else: parseStatements(elseBlock, depth + 1) });
            } else {
                throw new Error(text("missingBlock"));
            }
            block = block.getNextBlock();
        }
        return statements;
    }

    function compile() {
        if (!workspace) throw new Error(text("invalidWorkspace"));
        const top = workspace.getTopBlocks(true);
        if (top.length !== 1) throw new Error(text("invalidWorkspace"));
        const event = top[0];
        let trigger;
        if (event.type === "iot_when_parameter_changes") {
            trigger = { type: "parameter_changed", device: event.getFieldValue("DEVICE"), parameter: event.getFieldValue("PARAMETER") };
            if (event.getFieldValue("MATCH") === "equals") {
                trigger.value = parseTriggerValue(
                    event.getFieldValue("VALUE"), selectedParameter(event)
                );
            }
            const stableFor = Number(event.getFieldValue("FOR_SECONDS") || 0);
            const cooldown = Number(event.getFieldValue("COOLDOWN_SECONDS") || 0);
            if (stableFor > 0) trigger.for_seconds = stableFor;
            if (cooldown > 0) trigger.cooldown_seconds = cooldown;
        } else if (event.type === "iot_when_device_connects") {
            trigger = { type: "device_connected", device: event.getFieldValue("DEVICE") };
        } else if (event.type === "iot_when_device_disconnects") {
            trigger = { type: "device_disconnected", device: event.getFieldValue("DEVICE") };
        } else if (event.type === "iot_when_gateway_starts") {
            trigger = { type: "gateway_started" };
        } else {
            throw new Error(text("invalidWorkspace"));
        }
        const body = parseStatements(event.getNextBlock());
        if (!body.length) throw new Error(text("emptyBody"));
        return { version: 1, trigger, body };
    }

    function load(state) {
        if (!workspace) return;
        workspace.clear();
        if (state?.blocks) Blockly.serialization.workspaces.load(state, workspace);
        refreshParameterBlockFields();
        requestAnimationFrame(() => Blockly.svgResize(workspace));
        announceHistoryState();
    }

    function loadLegacy(config) {
        if (!workspace || !config?.trigger || !config?.target) return;
        const source = devices.find(item => item.device === config.trigger.device);
        const sourceParameter = source?.schema?.parameters?.[config.trigger.parameter];
        const target = devices.find(item => item.device === config.target.device);
        const targetDefinition = config.target.kind === "action"
            ? target?.schema?.actions?.[config.target.name]
            : target?.schema?.parameters?.[config.target.name];
        if (!sourceParameter || !targetDefinition) throw new Error(text("missingBlock"));
        workspace.clear();
        announceHistoryState();
        const root = workspace.newBlock("iot_when_parameter_changes");
        root.setFieldValue(config.trigger.device, "DEVICE");
        const triggerParameters = parameterOptions.call(root.getField("PARAMETER"));
        if (triggerParameters.some(option => option[1] === config.trigger.parameter)) {
            root.setFieldValue(config.trigger.parameter, "PARAMETER");
        }
        const branch = workspace.newBlock("controls_if");
        const compare = workspace.newBlock("logic_compare");
        const operator = { below: "LT", at_most: "LTE", above: "GT", at_least: "GTE" }[config.trigger.operator] || "LT";
        compare.setFieldValue(operator, "OP");
        const sensor = workspace.newBlock("iot_device_parameter");
        sensor.setFieldValue(config.trigger.device, "DEVICE");
        const sensorParameters = parameterOptions.call(sensor.getField("PARAMETER"));
        if (sensorParameters.some(option => option[1] === config.trigger.parameter)) {
            sensor.setFieldValue(config.trigger.parameter, "PARAMETER");
        }
        const threshold = workspace.newBlock("math_number");
        threshold.setFieldValue(Number(config.trigger.threshold), "NUM");
        compare.getInput("A").connection.connect(sensor.outputConnection);
        compare.getInput("B").connection.connect(threshold.outputConnection);
        branch.getInput("IF0").connection.connect(compare.outputConnection);
        root.nextConnection.connect(branch.previousConnection);

        if (config.target.kind === "action") {
            const action = workspace.newBlock("iot_invoke_action");
            action.setFieldValue(config.target.device, "DEVICE");
            const actions = actionOptions.call(action.getField("ACTION"));
            if (actions.some(option => option[1] === config.target.name)) {
                action.setFieldValue(config.target.name, "ACTION");
            }
            updateActionArguments(action);
            const definition = devices.find(item => item.device === config.target.device)?.schema?.actions?.[config.target.name];
            for (const [id, value] of Object.entries(config.target.arguments || {})) {
                const field = action.getField(`ARG_${id}`);
                const argument = definition?.arguments?.[id];
                if (!field || value === null) continue;
                const fieldValue = argument?.type === "enum"
                    ? JSON.stringify(value)
                    : argument?.type === "boolean" ? String(value) : String(value);
                field.setValue(fieldValue);
            }
            branch.getInput("DO0").connection.connect(action.previousConnection);
        } else {
            const action = workspace.newBlock("iot_set_parameter");
            action.setFieldValue(config.target.device, "DEVICE");
            const writableParameters = writableParameterOptions.call(action.getField("PARAMETER"));
            if (writableParameters.some(option => option[1] === config.target.name)) {
                action.setFieldValue(config.target.name, "PARAMETER");
            }
            let valueBlock;
            if (typeof config.target.value === "boolean") {
                valueBlock = workspace.newBlock("logic_boolean");
                valueBlock.setFieldValue(config.target.value ? "TRUE" : "FALSE", "BOOL");
            } else if (typeof config.target.value === "number") {
                valueBlock = workspace.newBlock("math_number");
                valueBlock.setFieldValue(config.target.value, "NUM");
            } else {
                valueBlock = workspace.newBlock("text");
                valueBlock.setFieldValue(String(config.target.value ?? ""), "TEXT");
            }
            action.getInput("VALUE").connection.connect(valueBlock.outputConnection);
            branch.getInput("DO0").connection.connect(action.previousConnection);
        }
        root.moveBy(35, 25);
        for (const block of workspace.getAllBlocks(false)) {
            block.initSvg();
            block.render();
        }
        announceHistoryState();
        resize();
    }

    function resize() {
        if (workspace) requestAnimationFrame(() => Blockly.svgResize(workspace));
    }

    window.AutomationBlockly = {
        initialize,
        load,
        loadLegacy,
        compile,
        getState: () => workspace ? Blockly.serialization.workspaces.save(workspace) : null,
        clear() {
            workspace?.clear();
            announceHistoryState();
        },
        undo() {
            workspace?.undo(false);
            announceHistoryState();
        },
        redo() {
            workspace?.redo();
            announceHistoryState();
        },
        resize,
        replaceDeviceId(oldId, newId) {
            if (typeof oldId !== "string" || typeof newId !== "string" || oldId === newId) return;
            if (workspace) stateBeforeDeviceRefresh = Blockly.serialization.workspaces.save(workspace);
            pendingDeviceIdRenames.set(oldId, newId);
        },
        update(nextDevices, nextLanguage) {
            const state = applyDeviceIdRenames(
                workspace ? Blockly.serialization.workspaces.save(workspace) : stateBeforeDeviceRefresh
            );
            stateBeforeDeviceRefresh = state;
            initialize("automation-workspace", nextDevices, nextLanguage, state);
        },
        isReady: () => Boolean(workspace)
    };
})();
