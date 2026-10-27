// This function executes inside the verified Electron main process.
export async function clientOperation(request) {
  const { app, BrowserWindow, screen } = process.mainModule.require('electron');
  const slot = '__mg400ClientAutomationV1';
  const session = globalThis[slot] ||= { busy: false, frame: null };
  if (session.busy) return session.cleanupUnconfirmed
    ? { ok: false, code: 'CLEANUP_UNCONFIRMED', error: 'The previous operation could not confirm input or focus cleanup. New input is blocked.' }
    : { ok: false, code: 'CLIENT_BUSY', error: 'The previous operation is still active.' };
  session.busy = true;
  let window, contents, attached = false, focusEmulated = false, rendererFocusPrepared = false, heldMouse = null;
  const heldKeys = [], completed = [], cleanupErrors = [];
  const releaseHistory = [];
  let lastMouse = null;
  const inFlight = new Set();
  let cancelled = false, uncertain = false, before, result;
  const deadline = Date.now() + 20000;
  const fail = (code, message) => { const error = new Error(message); error.code = code; throw error; };
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const cancel = () => { cancelled = true; };
  const bounded = (promise, ms = 3000) => new Promise((resolve, reject) => {
    const timer = setTimeout(() => { uncertain = true; const error = new Error('Client response timed out. Do not repeat input.'); error.code = 'ACTION_TIMEOUT'; reject(error); }, ms);
    promise.then(value => { clearTimeout(timer); resolve(value); }, error => { clearTimeout(timer); reject(error); });
  });
  const send = (method, params) => {
    const command = contents.debugger.sendCommand(method, params);
    inFlight.add(command);
    command.then(() => inFlight.delete(command), () => inFlight.delete(command));
    return bounded(command);
  };
  const evaluate = async expression => {
    const response = await send('Runtime.evaluate', { expression, returnByValue: true });
    if (response.exceptionDetails) fail('RENDERER_ERROR', response.exceptionDetails.text);
    return response.result.value;
  };
  const desktop = () => ({ focused: window.isFocused(), minimized: window.isMinimized(), visible: window.isVisible(), hidden: app.isHidden(), cursor: screen.getCursorScreenPoint() });
  const geometry = async () => ({
    pid: process.pid, windowId: window.id, contentsId: contents.id, url: contents.getURL(),
    bounds: window.getBounds(), contentBounds: window.getContentBounds(), zoom: contents.getZoomFactor(),
    renderer: await evaluate(`(()=>{const v=document.getElementById('remote-video'),r=v?.getBoundingClientRect(),s=v&&getComputedStyle(v);return {width:innerWidth,height:innerHeight,dpr:devicePixelRatio,timeOrigin:performance.timeOrigin,video:r?{x:r.x,y:r.y,width:r.width,height:r.height,intrinsicWidth:v.videoWidth,intrinsicHeight:v.videoHeight,ready:v.readyState,source:v.srcObject?.id||v.currentSrc,fit:s.objectFit,position:s.objectPosition}:null};})()`)
  });
  const videoProgress = () => evaluate(`(()=>{const v=document.getElementById('remote-video');return v?{time:v.currentTime,frames:v.getVideoPlaybackQuality().totalVideoFrames,ready:v.readyState}:null;})()`);
  const checkWindow = () => {
    if (cancelled || Date.now() > deadline) fail('WINDOW_CHANGED', 'Window focus or visibility changed. Capture again.');
    if (window.isMinimized()) fail('CLIENT_MINIMIZED', 'Client is minimized. No window change was made.');
    if (!window.isVisible() || app.isHidden()) fail('CLIENT_HIDDEN', 'Client is hidden. No window change was made.');
    if (window.isFocused() !== before.focused) fail('FOCUS_CHANGED', 'Window focus changed. Input stopped.');
  };
  const capture = async () => {
    const state = await geometry();
    const progress = await videoProgress();
    if (!state.renderer.video || state.renderer.video.ready < 2) fail('STREAM_UNAVAILABLE', 'The video stream is not ready.');
    const image = await evaluate(`(()=>{const v=document.getElementById('remote-video'),c=document.createElement('canvas');c.width=v.videoWidth;c.height=v.videoHeight;if(!c.width||!c.height)throw new Error('Video frame is unavailable.');c.getContext('2d').drawImage(v,0,0);return {width:c.width,height:c.height,png:c.toDataURL('image/png').split(',')[1]};})()`);
    if (!image?.png || !image.width || !image.height) fail('CAPTURE_EMPTY', 'Client returned an empty video frame.');
    if (JSON.stringify(state) !== JSON.stringify(await geometry())) fail('FRAME_CHANGED', 'Client geometry changed during capture. Capture again.');
    checkWindow();
    const frame = { id: process.mainModule.require('node:crypto').randomUUID(), createdAt: Date.now(), source: 'remote-video', geometry: state, videoProgress: progress, size: { width: image.width, height: image.height } };
    session.frame = frame;
    return { ...frame, png: image.png };
  };
  const checkFrame = async frame => {
    checkWindow();
    if (JSON.stringify(frame.geometry) !== JSON.stringify(await geometry())) fail('FRAME_CHANGED', 'The captured geometry changed. Capture again.');
    checkWindow();
    if (!frame.geometry.renderer.video || frame.geometry.renderer.video.ready < 2) fail('STREAM_UNAVAILABLE', 'The video stream is not ready.');
  };
  const keySpec = key => {
    const named = { Enter: [13, 'Enter'], Tab: [9, 'Tab'], Escape: [27, 'Escape'], Backspace: [8, 'Backspace'], Delete: [46, 'Delete'], ArrowLeft: [37, 'ArrowLeft'], ArrowUp: [38, 'ArrowUp'], ArrowRight: [39, 'ArrowRight'], ArrowDown: [40, 'ArrowDown'], Home: [36, 'Home'], End: [35, 'End'], Space: [32, 'Space'], Control: [17, 'ControlLeft'], Shift: [16, 'ShiftLeft'], Alt: [18, 'AltLeft'], Meta: [91, 'MetaLeft'] };
    if (key === ' ') key = 'Space';
    if (named[key]) return { key: key === 'Space' ? ' ' : key, code: named[key][1], windowsVirtualKeyCode: named[key][0] };
    if (/^[a-z]$/i.test(key)) return { key, code: `Key${key.toUpperCase()}`, windowsVirtualKeyCode: key.toUpperCase().charCodeAt(0), shift: key === key.toUpperCase() };
    const rows = [['1!', 'Digit1', 49], ['2@', 'Digit2', 50], ['3#', 'Digit3', 51], ['4$', 'Digit4', 52], ['5%', 'Digit5', 53], ['6^', 'Digit6', 54], ['7&', 'Digit7', 55], ['8*', 'Digit8', 56], ['9(', 'Digit9', 57], ['0)', 'Digit0', 48], ['-_', 'Minus', 189], ['=+', 'Equal', 187], ['[{', 'BracketLeft', 219], [']}', 'BracketRight', 221], ['\\|', 'Backslash', 220], [';:', 'Semicolon', 186], ['\'"', 'Quote', 222], [',<', 'Comma', 188], ['.>', 'Period', 190], ['/?', 'Slash', 191], ['`~', 'Backquote', 192]];
    const row = rows.find(([characters]) => characters.includes(key));
    if (!row) fail('UNSUPPORTED_KEY', 'The key has no supported keyboard mapping.');
    return { key, code: row[1], windowsVirtualKeyCode: row[2], shift: row[0][1] === key };
  };
  const releaseKeys = async () => {
    const bits = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };
    let firstError;
    for (const event of [...heldKeys].reverse()) {
      const mask = heldKeys.reduce((value, held) => value | (held === event ? 0 : bits[held.key] || 0), 0);
      try {
        await send('Input.dispatchKeyEvent', { type: 'keyUp', ...event, modifiers: mask });
        heldKeys.splice(heldKeys.indexOf(event), 1);
      } catch (error) { firstError ||= error; }
    }
    if (firstError) throw firstError;
  };
  const press = async (key, modifiers = []) => {
    const bits = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };
    const { shift, ...event } = keySpec(key);
    const characterShift = shift && !modifiers.some(modifier => ['Control', 'Alt', 'Meta'].includes(modifier));
    modifiers = [...new Set([...modifiers, ...(characterShift ? ['Shift'] : [])])];
    if (/^[a-z]$/i.test(event.key)) event.key = modifiers.includes('Shift') ? event.key.toUpperCase() : event.key.toLowerCase();
    if (modifiers.includes('Shift') && /^\d$/.test(event.key)) event.key = ')!@#$%^&*('[Number(event.key)];
    // The client sends single-character keys as text even during shortcuts.
    if (event.key.length === 1 && modifiers.some(modifier => ['Control', 'Alt', 'Meta'].includes(modifier))) event.key = 'Unidentified';
    let mask = 0;
    for (const modifier of modifiers) {
      mask |= bits[modifier];
      const event = keySpec(modifier);
      heldKeys.push(event);
      releaseHistory.push(event);
      uncertain = true;
      await send('Input.dispatchKeyEvent', { type: 'keyDown', ...event, modifiers: mask });
      checkWindow();
    }
    checkWindow();
    heldKeys.push(event);
    releaseHistory.push(event);
    uncertain = true;
    await send('Input.dispatchKeyEvent', { type: 'keyDown', ...event, modifiers: mask, autoRepeat: false });
    await pause(15);
    await releaseKeys();
    uncertain = false;
  };
  try {
    const windows = BrowserWindow.getAllWindows().filter(w => w.webContents.getURL() === 'file:///Applications/Isaac%20Sim%20WebRTC%20Streaming%20Client.app/Contents/Resources/app.asar/dist/renderer/index.html');
    if (windows.length !== 1) fail('CLIENT_IDENTITY', 'Expected one installed client window.');
    window = windows[0]; contents = window.webContents; before = desktop();
    if (contents.debugger.isAttached()) fail('DEBUGGER_BUSY', 'Another debugger is attached.');
    contents.debugger.attach('1.3'); attached = true;
    if (request.command === 'status') {
      result = { ok: true, state: before, geometry: await geometry(), videoProgress: await videoProgress() };
    } else {
      checkWindow();
      for (const event of ['focus', 'blur', 'minimize', 'hide', 'closed']) window.on(event, cancel);
      if (request.command === 'capture') result = { ok: true, state: before, frame: await capture() };
      else {
        const frame = session.frame;
        if (!frame || frame.id !== request.frame.id || Date.now() - frame.createdAt > 180000) fail('FRAME_EXPIRED', 'This capture is expired or already used. Capture again.');
        session.frame = null;
        await checkFrame(frame);
        const firstProgress = await videoProgress();
        await pause(150);
        const nextProgress = await videoProgress();
        if (!firstProgress || !nextProgress || nextProgress.frames <= firstProgress.frames) fail('STREAM_STALLED', 'The video did not deliver a new frame. No input was sent.');
        rendererFocusPrepared = true;
        await evaluate(`globalThis.__mg400ClientFocusV1={active:document.activeElement,pageFocused:document.hasFocus()};true`);
        focusEmulated = true;
        await send('Emulation.setFocusEmulationEnabled', { enabled: true });
        await evaluate(`(()=>{const v=document.getElementById('remote-video');if(v?.parentElement?.id!=='remote-video-input-parent')throw new Error('Streaming input target is unavailable.');v.focus({preventScroll:true});v.parentElement.dispatchEvent(new FocusEvent('focusin',{bubbles:true}));return true;})()`);
        for (const [index, action] of request.actions.entries()) {
          await checkFrame(frame);
          if (action.type === 'click') {
            if (action.x < 0 || action.y < 0 || action.x >= frame.size.width || action.y >= frame.size.height) fail('TARGET_OUTSIDE', 'Click is outside the captured video.');
            const video = frame.geometry.renderer.video;
            if (video.fit !== 'contain' || video.position !== '50% 50%') fail('VIDEO_LAYOUT', 'The video layout is not supported. No click was sent.');
            const scale = Math.min(video.width / video.intrinsicWidth, video.height / video.intrinsicHeight);
            const width = video.intrinsicWidth * scale;
            const height = video.intrinsicHeight * scale;
            const x = video.x + (video.width - width) / 2 + action.x * width / frame.size.width;
            const y = video.y + (video.height - height) / 2 + action.y * height / frame.size.height;
            if (!await evaluate(`document.elementFromPoint(${x},${y})===document.getElementById('remote-video')`)) fail('TARGET_COVERED', 'The target is outside the video or covered.');
            await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
            await pause(100);
            await checkFrame(frame);
            if (!await evaluate(`document.elementFromPoint(${x},${y})===document.getElementById('remote-video')`)) fail('TARGET_COVERED', 'The target became covered. No click was sent.');
            checkWindow();
            heldMouse = { x, y, button: 'left', clickCount: 1 };
            lastMouse = heldMouse;
            uncertain = true;
            await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...heldMouse, buttons: 1 });
            await pause(80);
            await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...heldMouse, buttons: 0 });
            heldMouse = null; uncertain = false;
          } else if (action.type === 'key') await press(action.key, action.modifiers);
          else {
            for (const char of action.text.replace(/\r\n?/g, '\n')) {
              checkWindow();
              await press(char === '\n' ? 'Enter' : char === '\t' ? 'Tab' : char);
            }
          }
          completed.push(index);
          await pause(100);
        }
        checkWindow();
        result = { ok: true, completed, verification: 'Input was dispatched. Check the remote result in the returned image.', frame: await capture() };
      }
    }
  } catch (error) {
    result = { ok: false, code: error.code || 'CLIENT_ERROR', error: error.message, completed, outcomeUnknown: uncertain };
  } finally {
    if (attached) {
      if (heldMouse) try { await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...heldMouse, buttons: 0 }); } catch (error) { cleanupErrors.push(error.message); }
      try { await releaseKeys(); } catch (error) { cleanupErrors.push(error.message); }
      if (rendererFocusPrepared) try {
        const restoreActive = !cancelled && window.isFocused() === before.focused;
        const restoreBackground = !before.focused && !window.isFocused();
        await evaluate(`(()=>{const s=globalThis.__mg400ClientFocusV1;delete globalThis.__mg400ClientFocusV1;if(!s)return true;if(${restoreActive}&&s.active?.isConnected){const v=document.getElementById('remote-video');if(document.activeElement===v)v.blur();s.active.focus({preventScroll:true});}if(${restoreBackground}&&!s.pageFocused){document.getElementById('remote-video-input-parent')?.dispatchEvent(new FocusEvent('focusout',{bubbles:true}));window.dispatchEvent(new Event('blur'));}return true;})()`);
      } catch (error) { cleanupErrors.push(error.message); }
      if (focusEmulated) try { await send('Emulation.setFocusEmulationEnabled', { enabled: false }); } catch (error) { cleanupErrors.push(error.message); }
      if (!inFlight.size) try { contents.debugger.detach(); } catch (error) { cleanupErrors.push(error.message); }
    }
    if (window && !window.isDestroyed()) {
      for (const event of ['focus', 'blur', 'minimize', 'hide', 'closed']) window.removeListener(event, cancel);
      result.before = before; result.after = desktop();
    }
    if (cleanupErrors.length) {
      if (result.ok) { result.code = 'CLEANUP_UNCONFIRMED'; result.error = 'Input or focus cleanup could not be confirmed. New input is blocked.'; }
      result.ok = false; result.cleanupErrors = cleanupErrors; result.outcomeUnknown = true;
      session.cleanupUnconfirmed = true;
    }
    if (!result.ok) session.frame = null;
    // A timed-out command can still finish. Block new operations until it settles.
    if (inFlight.size) {
      result.ok = false; result.outcomeUnknown = true;
      result.code = 'ACTION_PENDING';
      result.error = 'A client command is still pending. Input will not be repeated.';
      Promise.allSettled([...inFlight]).then(async () => {
        const attempt = async action => {
          try { await action(); } catch { session.cleanupUnconfirmed = true; }
        };
        if (lastMouse) await attempt(() => send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...lastMouse, buttons: 0 }));
        for (const event of [...releaseHistory].reverse()) await attempt(() => send('Input.dispatchKeyEvent', { type: 'keyUp', ...event, modifiers: 0 }));
        if (focusEmulated) await attempt(() => send('Emulation.setFocusEmulationEnabled', { enabled: false }));
        if (!inFlight.size) await attempt(() => contents.debugger.detach());
        if (!session.cleanupUnconfirmed && !inFlight.size) session.busy = false;
      });
    } else if (!session.cleanupUnconfirmed) session.busy = false;
  }
  return result;
}
