// Fondo «Cerbero Cyber» (lluvia de código y suelo) y botón de modo sereno.
// Sereno por defecto: un solo fotograma, sin animar. El modo completo anima (salvo prefers-reduced-motion).
(() => {
  const rm = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const KEY = 'cerbero_calm';
  const root = document.documentElement;
  const calm = () => root.hasAttribute('data-calm');
  const store = (v) => {
    try {
      localStorage.setItem(KEY, v);
    } catch (e) {
      // Sin almacenamiento: dura lo que la pestaña.
    }
  };
  let full = false;
  try {
    full = localStorage.getItem(KEY) === '0';
  } catch (e) {
    // Sin almacenamiento: modo sereno.
  }
  root.toggleAttribute('data-calm', !full);

  const c = document.createElement('canvas');
  c.id = 'bg';
  c.setAttribute('aria-hidden', 'true');
  document.body.prepend(c);
  const x = c.getContext('2d');
  const G = 'ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿ01234567890ABCDEF<>/#$%';
  let W, H, cols, drops, running = false;

  const size = () => {
    W = c.width = innerWidth;
    H = c.height = innerHeight;
    cols = Math.ceil(W / 18);
    drops = Array.from({ length: cols }, () => Math.random() * -60);
    x.fillStyle = '#03050c';
    x.fillRect(0, 0, W, H);
    if (calm() || rm) for (let i = 0; i < 40; i++) draw();
  };

  const draw = () => {
    x.fillStyle = 'rgba(3,5,12,.16)';
    x.fillRect(0, 0, W, H);
    x.font = '15px "Share Tech Mono",monospace';
    for (let i = 0; i < cols; i++) {
      const y = drops[i] * 18;
      x.fillStyle = Math.random() < 0.04 ? '#ff2e88' : '#00c4d4';
      x.globalAlpha = 0.55;
      x.fillText(G[(Math.random() * G.length) | 0], i * 18, y);
      if (y > H && Math.random() > 0.975) drops[i] = Math.random() * -20;
      drops[i] += 0.45;
    }
    x.globalAlpha = 1;
    const hz = H * 0.74;
    const t = (performance.now() / 1000) % 1;
    x.strokeStyle = 'rgba(122,61,240,.55)';
    x.beginPath();
    for (let i = -14; i <= 14; i++) {
      x.moveTo(W / 2 + i * 12, hz);
      x.lineTo(W / 2 + (i * W) / 9, H);
    }
    x.stroke();
    for (let j = 0; j < 9; j++) {
      const y = hz + (H - hz) * ((j + t) / 9) ** 2;
      x.beginPath();
      x.moveTo(0, y);
      x.lineTo(W, y);
      x.stroke();
    }
  };

  const loop = () => {
    if (calm() || rm || document.hidden) {
      running = false;
      return;
    }
    draw();
    requestAnimationFrame(loop);
  };
  const start = () => {
    if (!running && !calm() && !rm) {
      running = true;
      loop();
    }
  };

  size();
  addEventListener('resize', size);
  document.addEventListener('visibilitychange', start);
  start();

  document.getElementById('theme').addEventListener('click', () => {
    const on = !calm();
    root.toggleAttribute('data-calm', on);
    store(on ? '1' : '0');
    start();
  });
})();
