"""Run manual-zero UI against the existing fake DOM and fake HTTP transport."""

import subprocess

import pytest
from test_stage_homing_ui import HARNESS, SCRIPT

ZERO_HARNESS = (
    HARNESS.split("const scenarios = {", 1)[0]
    .replace(
        "const window = new Element(), requests",
        "const window = new Element(); window.confirm = () => true; const requests",
    )
    .replace(
        "node, cards, requests, deferred, timers,",
        "node, cards, requests, deferred, timers, window,",
    )
    .replace(
        "if (url === failPath)",
        "if (url.endsWith('/reference')) return Promise.resolve(reply({"
        "image:'data:image/jpeg;base64,YWJj', offset_px:[1.25,-2.5]}));\n"
        "      if (url === failPath)",
    )
    + r"""
function manualZero(time = 2) {
  const next = snapshot(time);
  next.feedback_fresh = true;
  next.motors.forEach(motor => Object.assign(motor, {
    homing_available:false, zero_available:true,
    reference:{stream:'camera3', marker:12, pixel:[99.5,99.5], tolerance_px:2},
  }));
  return next;
}
const scenarios = {
  async zero_requires_confirm_and_calls_only_zero(page) {
    page.render(manualZero());
    const card = page.cards[2], zero = card.querySelector('.zero-button');
    assert.equal(zero.hidden, false);
    assert.equal(zero.disabled, false);
    assert.equal(page.home('insert').hidden, true);
    assert.equal(card.querySelector('.homing-status').textContent, 'Zero required');
    page.window.confirm = () => false;
    await zero.emit('click');
    assert.equal(page.posts().length, 0);
    page.window.confirm = () => true;
    await zero.emit('click');
    assert.deepEqual(page.posts().map(item => [item.url, JSON.parse(item.body)]), [
      ['/api/motors/insert/zero', {confirm:true}],
    ]);
    const confirmed = manualZero(3);
    confirmed.motors[2].automation_ready = true;
    page.render(confirmed);
    assert.equal(card.querySelector('.homing-status').textContent, 'Zero confirmed');
  },
  async teach_requires_a_separate_confirmed_click(page) {
    page.render(manualZero());
    const teach = page.cards[2].querySelector('.reference-teach');
    page.window.confirm = () => false;
    await teach.emit('click');
    assert.equal(page.posts().length, 0);
    page.window.confirm = () => true;
    await teach.emit('click');
    assert.deepEqual(page.posts().map(item => [item.url, JSON.parse(item.body)]), [
      ['/api/motors/insert/teach_reference', {confirm:true}],
    ]);
  },
  async stale_fault_lease_and_motion_disable_zero_and_teach(page) {
    const alter = [next => {next.feedback_fresh = false;}, next => {next.locked = true;},
      next => {next.lease.holder='automation';}, next => {next.motors[2].available=false;},
      next => {next.automation.active={action:'home', motor:'top'};}];
    let time = 2;
    for (const change of alter) {
      const next = manualZero(time++); change(next); page.render(next);
      for (const name of ['.zero-button','.reference-teach']) {
        const button = page.cards[2].querySelector(name);
        assert.equal(button.disabled,true);
        await button.emit('click');
      }
    }
    assert.equal(page.posts().length,0);
  },
  async pending_zero_allows_stop_and_never_retries(page) {
    page.render(manualZero());
    page.defer('/api/motors/insert/zero');
    const pending = page.cards[2].querySelector('.zero-button').emit('click');
    await flush();
    assert.equal(page.cards[0].querySelector('.zero-button').disabled,true);
    await page.node('#stop-all').emit('click');
    page.deferred.shift()(manualZero(3)); await pending;
    assert.equal(page.posts().filter(item => item.url.endsWith('/zero')).length,1);
    assert.equal(page.posts().filter(item => item.url === '/api/stop').length,1);
  },
  async preview_uses_authenticated_api_and_clears_on_disconnect(page) {
    page.render(manualZero());
    const card = page.cards[2];
    await card.querySelector('.reference-refresh').emit('click');
    assert.equal(card.querySelector('.reference-image').src,'data:image/jpeg;base64,YWJj');
    assert.equal(card.querySelector('.reference-image').hidden,false);
    assert.equal(card.querySelector('.reference-offset').textContent,'Marker offset: 1.3, -2.5 px');
    const next = manualZero(3); next.connected = false; page.render(next);
    assert.equal(card.querySelector('.reference-image').hidden,true);
    assert.equal(card.querySelector('.reference-image').src,'');
  },
  async switch_board_preserves_home_and_legacy_hides_zero(page) {
    const next = manualZero(); next.motors.forEach(motor => {motor.homing_available=true;});
    page.render(next);
    assert.equal(page.home('insert').hidden,false);
    assert.equal(page.home('insert').disabled,false);
    assert.equal(page.cards[2].querySelector('.zero-button').hidden,true);
    const old = snapshot(3); page.render(old);
    assert.equal(page.cards[2].querySelector('.zero-button').hidden,true);
    assert.equal(page.cards[2].querySelector('.zero-button').disabled,true);
  },
};
(async () => { await scenarios[process.argv[2]](await browser()); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
)


@pytest.mark.parametrize(
    "scenario",
    [
        "zero_requires_confirm_and_calls_only_zero",
        "teach_requires_a_separate_confirmed_click",
        "stale_fault_lease_and_motion_disable_zero_and_teach",
        "pending_zero_allows_stop_and_never_retries",
        "preview_uses_authenticated_api_and_clears_on_disconnect",
        "switch_board_preserves_home_and_legacy_hides_zero",
    ],
)
def test_stage_manual_zero_controls(scenario):
    result = subprocess.run(
        ["node", "-e", ZERO_HARNESS, str(SCRIPT), scenario],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
