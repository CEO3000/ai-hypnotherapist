// "Digital rain" of green glyphs on a canvas. Used full-screen behind the avatar, and small as the code inside it.

const GLYPHS = "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン0123456789";

export function createRain(canvas, { fontSize = 16, fps = 22, fade = 0.08, background = "10, 18, 16",
                                     color = "#22c55e", head = "#bbf7d0", speed = 1 } = {}) {
  const ctx = canvas.getContext("2d");
  let columns = [];
  let running = true;
  let last = 0;
  let rate = speed;

  function resize() {
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    const { width, height } = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.floor(width * ratio));
    canvas.height = Math.max(1, Math.floor(height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    const count = Math.ceil(width / fontSize);
    columns = Array.from({ length: count }, (_, i) => columns[i] ?? Math.random() * -height / fontSize);
    ctx.fillStyle = `rgb(${background})`;
    ctx.fillRect(0, 0, width, height);
  }

  function draw(now) {
    if (!running) return;
    requestAnimationFrame(draw);
    if (now - last < 1000 / fps) return;
    last = now;
    const { width, height } = canvas.getBoundingClientRect();
    ctx.fillStyle = `rgba(${background}, ${fade})`;
    ctx.fillRect(0, 0, width, height);
    ctx.font = `${fontSize}px monospace`;
    columns.forEach((y, i) => {
      const glyph = GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
      const x = i * fontSize;
      ctx.fillStyle = Math.random() < 0.08 ? head : color;
      ctx.fillText(glyph, x, y * fontSize);
      columns[i] = y * fontSize > height && Math.random() > 0.975 ? 0 : y + rate * (0.6 + (i % 3) * 0.2);
    });
  }

  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  resize();
  requestAnimationFrame(draw);
  return {
    setSpeed(value) { rate = value; },
    stop() { running = false; observer.disconnect(); },
  };
}
