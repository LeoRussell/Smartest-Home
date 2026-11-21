# Blockly vendor files

This directory contains the browser distribution of Blockly **13.4.0** from the
`blockly` npm package. The bundled core reports this version as `Blockly.VERSION`.

- `blockly_compressed.js`, `blocks_compressed.js`: Blockly core and standard blocks.
- `msg/en.js`, `msg/ru.js`: English and Russian message bundles.
- `media/`: Blockly workspace assets.
- [LICENSE](LICENSE): upstream Apache License 2.0.

The project vendors these files so the static web app does not require a CDN, npm, or a build step at runtime.

[index.html](../../index.html) loads the core and standard blocks first, then
captures the English and Russian messages in `window.BlocklyLocales` before
loading the application integration. Switching the interface language uses these
local messages without downloading another bundle.

The workspace in [automation-blockly.js](../../automation-blockly.js) uses
`vendor/blockly/media/` relative to the web root. Serve the full `web/` directory,
including these assets, when opening the interface locally or over LAN.

Project-specific blocks, device menus and compilation belong in
`web/automation-blockly.js`, outside this vendor directory. The compressed bundles
are generated upstream files. If upgrading Blockly, keep the core, standard
blocks, locales, media and license from the same distribution together; verify
the application integration and isolated browser checks afterward. See the
[web interface guide](../../README.md) for the test command.
