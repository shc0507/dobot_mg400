// Compatibility entry point. Use the capture/act interface from isaac_client.mjs.
import { main } from './isaac_client.mjs';

main().catch(error => {
  console.error(JSON.stringify({ ok: false, error: error.message }));
  process.exitCode = 1;
});
