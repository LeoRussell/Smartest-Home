# Blockly vendor files

This directory contains the browser distribution of Blockly 13.4.0 from the `blockly` npm package.

- `blockly_compressed.js`, `blocks_compressed.js`: Blockly core and standard blocks.
- `msg/`: Russian and English message bundles.
- `media/`: Blockly workspace assets.
- `LICENSE`: upstream Apache 2.0 license.

The project vendors these files so the static web app does not require a CDN, npm, or a build step at runtime.
