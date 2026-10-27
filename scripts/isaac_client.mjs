// Capture and control the client without desktop input. Requires Node.js 22+.
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { withInspector } from './isaac_client_transport.mjs';
import { clientOperation } from './isaac_client_runtime.mjs';

export function validateActions(actions) {
  if (!Array.isArray(actions) || !actions.length || actions.length > 8) throw new Error('Use one to eight actions per batch.');
  let characters = 0;
  for (const action of actions) {
    if (!action || typeof action !== 'object') throw new Error('Each action must be an object.');
    if (action.type === 'click') {
      if (![action.x, action.y].every(Number.isFinite) || action.x < 0 || action.y < 0) throw new Error('Click coordinates must be nonnegative numbers.');
    } else if (action.type === 'key') {
      if (typeof action.key !== 'string' || !/^(?:[A-Za-z0-9]|Enter|Tab|Escape|Backspace|Delete|ArrowLeft|ArrowRight|ArrowUp|ArrowDown|Home|End|Space)$/.test(action.key)) throw new Error('Unsupported key.');
      if (action.modifiers !== undefined && (!Array.isArray(action.modifiers) || action.modifiers.some(key => !['Control', 'Shift', 'Alt', 'Meta'].includes(key)) || new Set(action.modifiers).size !== action.modifiers.length)) throw new Error('Invalid key modifiers.');
    } else if (action.type === 'text') {
      if (typeof action.text !== 'string' || !/^[\t\n\r\x20-\x7e]*$/.test(action.text)) throw new Error('Text supports ASCII, Tab, and newline.');
      characters += action.text.length;
    } else throw new Error('Use click, key, or text actions.');
  }
  if (characters > 256) throw new Error('Use at most 256 text characters per batch.');
  return actions;
}

export async function runClient(request) {
  if (!['status', 'capture', 'act'].includes(request.command)) throw new Error('Use status, capture, or act.');
  if (request.command === 'act') {
    validateActions(request.actions);
    if (typeof request.frame?.id !== 'string') throw new Error('Use the JSON file from capture.');
  }
  return withInspector(async rpc => {
    const result = await rpc('Runtime.evaluate', {
      expression: `(${clientOperation.toString()})(${JSON.stringify(request)})`,
      returnByValue: true, awaitPromise: true
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  });
}

export async function main(argv = process.argv.slice(2)) {
  const [command, first, second, third, ...extra] = argv;
  if (extra.length || (command === 'status' && first) || (command === 'capture' && (!first || second)) || (command === 'act' && (!first || !second || !third)) || !['status', 'capture', 'act'].includes(command)) {
    throw new Error('Usage: isaac_client.mjs status | capture OUTPUT_PREFIX | act FRAME.json ACTIONS.json OUTPUT_PREFIX');
  }
  const request = { command };
  if (command === 'act') {
    request.frame = JSON.parse(readFileSync(first, 'utf8'));
    request.actions = JSON.parse(readFileSync(second, 'utf8'));
  }
  const result = await runClient(request);
  if (result.frame) {
    const prefix = resolve(command === 'capture' ? first : third);
    const { png, ...frame } = result.frame;
    writeFileSync(`${prefix}.png`, Buffer.from(png, 'base64'));
    writeFileSync(`${prefix}.json`, JSON.stringify(frame, null, 2) + '\n');
    result.frame = { ...frame, image: `${prefix}.png`, metadata: `${prefix}.json` };
  }
  console.log(JSON.stringify(result));
  if (!result.ok) process.exitCode = 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main().catch(error => { console.error(JSON.stringify({ ok: false, error: error.message })); process.exitCode = 1; });
}
