/* P0-1 手机视口验收：/portal/report 三通道页面。
   与实际 DOM 对齐：汇总条为 #summary [data-c=text/photo/voice]，
   语音状态文字在 #mic .p-mic-t，录音两次点击（idle→授权 ready→recording），
   结束后 #micDone 显示试听/重录/删除。逐状态截图。 */
const { chromium } = require('playwright-core');
const path = require('path');
const fs = require('fs');

const BASE = 'http://127.0.0.1:5001';
const OUT = 'E:/program/decrease a/bishe/output/mobile/portal_combo';

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({
    channel: 'msedge',
    args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
           '--allow-file-access-from-files'],
  });
  const ctx = await browser.newContext({
    viewport: { width: 390, height: 844 },
    deviceScaleFactor: 2,
    isMobile: true,
    hasTouch: true,
    locale: 'zh-CN',
  });
  // headless 无物理麦克风：用 Web Audio 造一路真实音频流（Oscillator→MediaStreamDestination），
  // MediaRecorder 可正常采集，走通 ready→recording→done 全链路。
  await ctx.addInitScript(() => {
    navigator.mediaDevices.getUserMedia = async function () {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ac = new Ctx();
      const dest = ac.createMediaStreamDestination();
      const osc = ac.createOscillator();
      osc.frequency.value = 440;
      osc.connect(dest);
      osc.start();
      if (ac.state === 'suspended') await ac.resume();
      return dest.stream;
    };
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(15000);

  await page.goto(BASE + '/portal/report', { waitUntil: 'networkidle' }).catch((e) => {
    console.log('FAIL 页面打不开: ' + e.message);
    process.exit(1);
  });

  const readSummary = () => page.evaluate(() => Array.from(
    document.querySelectorAll('#summary [data-c]')).map((x) =>
      (x.className.includes('off') ? 'off:' : 'on:') + x.textContent));

  // 1) 初始态：无横向溢出 + 汇总条三灰 + 文字上限 1500 + 照片上限 10
  const m0 = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    mic: !!document.getElementById('mic'),
    summary: !!document.getElementById('summary'),
    maxLen: (document.getElementById('text') || {}).maxLength || -1,
    photoHint: (document.querySelector('label[for=screenshots]') || {}).textContent || '',
    maxPhotosJS: /\bMAX_PHOTOS\s*=\s*10\b/.test(document.querySelector('script').textContent || '') ||
                 Array.from(document.scripts).some((s) => /MAX_PHOTOS\s*=\s*10/.test(s.textContent)),
  }));
  const segs0 = await readSummary();
  const ok0 = m0.scrollW <= m0.innerW + 1 && m0.mic && m0.summary && m0.maxLen === 1500;
  console.log((ok0 ? 'OK  ' : '问题') + ' 初始态 scrollW=' + m0.scrollW + '/' + m0.innerW +
    ' 汇总=[' + segs0.join(' ') + '] maxlength=' + m0.maxLen +
    ' 照片提示="' + m0.photoHint.trim().slice(0, 24) + '" js-photos=10:' + m0.maxPhotosJS);
  await page.screenshot({ path: path.join(OUT, '1-initial.png') });

  // 2) 填文字 → 汇总条文字段点亮
  await page.fill('#text', '5栋楼下垃圾桶满溢，请尽快处理。');
  const segs1 = await readSummary();
  console.log('填文字后 汇总=[' + segs1.join(' ') + ']');
  await page.screenshot({ path: path.join(OUT, '2-text.png') });

  // 3) 授权麦克风第一次点击：idle → ready
  await ctx.grantPermissions(['microphone'], { origin: BASE });
  await page.click('#mic');
  await page.waitForTimeout(700);
  const ready = await page.evaluate(() => ({
    state: document.querySelector('#mic .p-mic-t').textContent,
    sub: document.querySelector('#mic .p-mic-s').textContent,
  }));
  console.log('第1次点录音：按钮="' + ready.state + '" 副标题="' + ready.sub + '"');
  await page.screenshot({ path: path.join(OUT, '3-ready.png') });

  // 4) 第二次点击：ready → recording，1.5s 后应显示计时
  await page.click('#mic');
  await page.waitForTimeout(1600);
  const rec = await page.evaluate(() => ({
    btn: document.querySelector('#mic .p-mic-t').textContent,
    recCls: document.getElementById('mic').className,
  }));
  console.log('第2次点录音：按钮计时="' + rec.btn + '" rec类=' + rec.recCls);
  await page.screenshot({ path: path.join(OUT, '4-recording.png') });

  // 5) 停止（recording 态再点一次）→ done
  await page.click('#mic');
  await page.waitForTimeout(500);
  const done = await page.evaluate(() => ({
    btn: document.querySelector('#mic .p-mic-t').textContent,
    doneShown: !document.getElementById('micDone').hidden,
    hasPlayback: !!document.getElementById('playback').src,
    redo: !!document.getElementById('micRedo'),
    clear: !!document.getElementById('micClear'),
    lenText: document.getElementById('micLen').textContent,
  }));
  const segs5 = await readSummary();
  console.log('停止后：按钮="' + done.btn + '" done卡片=' + done.doneShown +
    ' 试听src=' + done.hasPlayback + ' 重录=' + done.redo + ' 删除=' + done.clear +
    ' 时长=' + done.lenText + ' 汇总=[' + segs5.join(' ') + ']');
  await page.screenshot({ path: path.join(OUT, '5-recorded.png') });

  // 6) 拒绝权限路径：无 fake 设备/权限的独立浏览器实例，确保 getUserMedia 被拒
  const browser2 = await chromium.launch({ channel: 'msedge' });
  const ctx2 = await browser2.newContext({
    viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, locale: 'zh-CN',
  });
  const p2 = await ctx2.newPage();
  await p2.goto(BASE + '/portal/report', { waitUntil: 'networkidle' });
  try {
    await p2.click('#mic');
    await p2.waitForTimeout(1200);
    const denied = await p2.evaluate(() => {
      const d = document.getElementById('micDenied');
      return d ? { shown: !d.hidden, text: d.textContent.trim().slice(0, 28) } : { shown: false };
    });
    const btnState = await p2.evaluate(() => ({
      disabled: document.getElementById('mic').disabled,
      label: document.querySelector('#mic .p-mic-t').textContent,
    }));
    console.log('未授权点录音 → 引导卡显示=' + denied.shown + ' "' + denied.text +
      '" 按钮禁用=' + btnState.disabled + '("' + btnState.label + '")');
    await p2.screenshot({ path: path.join(OUT, '6-denied.png') });
  } catch (e) {
    console.log('拒绝路径跳过: ' + e.message);
  }
  await ctx2.close();
  await browser2.close();

  await browser.close();
  console.log('\n===== 手机视口验收完成，截图见 ' + OUT + ' =====');
})();