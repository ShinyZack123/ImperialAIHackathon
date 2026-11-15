const palette = {
  paper: '#f3f5f0', ink: '#192923', soft: '#52665d',
  line: '#d7e1d7', dark: '#172b24', green: '#205f4a', amber: '#95500e',
};

export async function recordWalkthrough({ scene, setStep, getView }) {
  const output = document.createElement('canvas');
  output.width = 1280;
  output.height = 720;
  const context = output.getContext('2d');
  const formats = ['video/mp4', 'video/webm;codecs=vp9', 'video/webm'];
  const mimeType = formats.find((format) => MediaRecorder.isTypeSupported(format));
  if (!mimeType) throw new Error('This browser cannot record the walkthrough.');
  const stream = output.captureStream(30);
  const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 4300000 });
  const chunks = [];
  const video = new Promise((resolve, reject) => {
    recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = () => resolve(new Blob(chunks, { type: mimeType }));
    recorder.onerror = () => reject(new Error('Browser recording stopped unexpectedly.'));
  });
  let lastStep = -1;
  const start = performance.now();
  const steps = [
    [0, 'Three racks and 24 GPUs'],
    [2300, 'Focus Rack B'],
    [4900, 'Slide out GPU-016'],
    [9200, 'Isolate GPU-001'],
    [12900, 'Fit the whole hall'],
  ];

  function text(value, x, y, size = 14, color = palette.ink, bold = false, mono = false) {
    context.fillStyle = color;
    context.font = (bold ? '700 ' : '400 ') + size + 'px ' + (mono ? 'monospace' : 'Helvetica Neue, Arial, sans-serif');
    context.fillText(value, x, y);
  }
  function box(x, y, w, h, fill) {
    context.fillStyle = fill;
    context.fillRect(x, y, w, h);
  }

  function render(source, time) {
    const passed = time - start;
    const step = Math.max(0, steps.findLastIndex(([offset]) => passed >= offset));
    if (step !== lastStep) {
      lastStep = step;
      setStep(step);
    }
    const view = getView();
    box(0, 0, 1280, 720, palette.paper);
    box(0, 0, 1280, 60, palette.dark);
    text('Ithil', 36, 37, 18, '#f3f8f1', true);
    text('Replay demo', 1133, 36, 12, '#cce3d4', false, true);
    text('Rack explorer', 36, 123, 49, palette.ink, true);
    text(view.replayLabel + ' · Synthetic', 962, 95, 13, palette.soft, false, true);
    text(view.time, 1110, 120, 13, palette.green, true, true);
    box(36, 148, 1208, 56, '#fff');
    const metrics = [
      ['Observed GPUs', view.observed],
      ['GPU power', view.power],
      ['Mean GPU temp', view.temp],
      ['Detector findings', String(view.findings)],
    ];
    for (const [i, [label, value]] of metrics.entries()) {
      const x = 51 + 302 * i;
      if (i) box(x - 15, 148, 1, 56, palette.line);
      text(label, x, 167, 11, palette.soft);
      text(value, x, 190, 22, i === 3 ? palette.amber : palette.ink, true);
    }

    const sceneBox = { x: 36, y: 220, w: 846, h: 444 };
    box(sceneBox.x, sceneBox.y, sceneBox.w, sceneBox.h, '#1e3028');
    const scale = Math.min(sceneBox.w / source.width, sceneBox.h / source.height);
    const imageW = source.width * scale;
    const imageH = source.height * scale;
    const x = sceneBox.x + (sceneBox.w - imageW) / 2;
    const y = sceneBox.y + (sceneBox.h - imageH) / 2;
    context.drawImage(source, x, y, imageW, imageH);
    for (const label of document.querySelectorAll('.rack-label:not([hidden])')) {
      const px = x + parseFloat(label.style.left) / source.clientWidth * imageW;
      const py = y + parseFloat(label.style.top) / source.clientHeight * imageH;
      box(px - 31, py - 19, 62, 18, palette.dark);
      text(label.textContent, px - 23, py - 6, 11, label.classList.contains('fault') ? '#ff928a' : label.classList.contains('warning') ? '#ffd4a2' : '#edf5eb', false, true);
    }
    box(54, 239, 292, 27, palette.dark);
    text(steps[step][1], 63, 257, 13, '#e7f5e9', true);

    box(899, 220, 345, 444, '#fff');
    text('Inspector', 916, 245, 13, palette.soft, true);
    text(view.title, 916, 286, 30, palette.ink, true);
    text(view.meta, 916, 309, 12, palette.soft, false, true);
    for (const [i, [label, value]] of view.metrics.entries()) {
      const x = 916 + (i % 2) * 157;
      const y = 344 + Math.floor(i / 2) * 53;
      box(x, y + 24, 145, 1, palette.line);
      text(label, x, y, 11, palette.soft);
      text(value, x, y + 19, 16, palette.ink, true);
    }
    if (view.chart) {
      text('Temperature · last 5 min', 916, 502, 11, palette.soft, true);
      context.drawImage(view.chart, 916, 509, 304, 62);
    }
    if (view.evidence) {
      box(914, 590, 314, 53, '#ffede8');
      text(view.evidence, 925, 612, 14, '#b3352b', true);
      text('Red outline identifies the affected GPU.', 925, 632, 12, '#6c4945');
    }
    text('Saved Databricks replay · Replay readings are synthetic. Tray motion is illustrative.', 36, 692, 12, palette.soft, false, true);
    if (passed >= 15700 && recorder.state === 'recording') {
      scene.setFrameSink(null);
      recorder.stop();
    }
  }

  recorder.start();
  scene.setFrameSink(render);
  try {
    const blob = await video;
    const url = URL.createObjectURL(blob);
    const download = document.createElement('a');
    download.href = url;
    download.download = 'A02-Rack-Walkthrough.' + (mimeType.startsWith('video/mp4') ? 'mp4' : 'webm');
    download.textContent = 'Download recording';
    return download;
  } finally {
    scene.setFrameSink(null);
    stream.getTracks().forEach((track) => track.stop());
  }
}
