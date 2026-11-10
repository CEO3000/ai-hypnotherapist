// The consultant avatar: the practice's own picture (a green android, made with Google Gemini), cut out of its
// background so it stands inside the app's matrix rain, and moved like a person: the jaw opens with the voice,
// syllable by syllable; while speaking the head nods on a stressed syllable and shifts slightly between phrases;
// while listening it leans in and nods when the client finishes a sentence; while thinking it glances away and
// holds; at rest it is still, just breathing. The eyes never blink and never move on their own (the practice's
// choice): they move with the head, and glow a little with the voice.
//
// The movement is made by bending the picture smoothly on the graphics card (three.js): the head turns about the
// neck while the shoulders stay put, and the lower jaw moves down below the mouth line. The same interface as the 3D
// head (avatar.js), so the consultation page works with either.

import * as THREE from "./vendor/three/three.module.js";

const IMAGE_URL = "/static/avatar/android.webp"; // 1230 x 905: the whole bust, transparent around the android
// Measured on the picture (pixels, y downwards).
const W = 1230;
const H = 905;
const HEAD = { x: 617, y: 341, rx: 220, ry: 306 }; // the head's outline (an oval)
const PIVOT = { x: 617, y: 625 }; // where the head turns on the neck
const NECK_FROM = 585; // above this the head moves fully; below NECK_TO (the collar) it stays
const NECK_TO = 690;
const MOUTH = { y: 542, halfWidth: 58 }; // the mouth line
const CHIN_Y = 647;
const JAW_HALF_WIDTH = 125;
const EYES = [[522.3, 356.1], [704.4, 351.3]];
const EYE_RADIUS = 30;
const SYLLABLE_MS = 175; // one syllable at speech rate 1.0
const f = (value) => Number(value).toFixed(2); // a number written for a shader (always a decimal)

export function syllables(word) {
  const letters = word.toLowerCase().replace(/[^a-z]/g, "");
  if (!letters) return 0;
  const groups = letters.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, "").match(/[aeiouy]+/g);
  return Math.max(1, groups ? groups.length : 1);
}

const VERTEX = /* glsl */ `
  uniform float uJaw;
  uniform vec3 uHead; // pitch (nod: negative is down), yaw (turn), roll (tilt), in radians
  uniform float uBreath;
  varying vec2 vUv;
  varying vec2 vPicture;

  void main() {
    vUv = uv;
    vec2 p = vec2(position.x + ${f(W / 2)}, ${f(H / 2)} - position.y); // picture pixels, y down
    vPicture = p;
    // The lower jaw: below the mouth line, within the jaw, down to the chin; the neck below takes up the stretch.
    float below = smoothstep(${f(MOUTH.y - 3)}, ${f(MOUTH.y + 9)}, p.y);
    float aboveNeck = 1.0 - smoothstep(${f(CHIN_Y)}, ${f(CHIN_Y + 55)}, p.y);
    float inJaw = 1.0 - smoothstep(${f(JAW_HALF_WIDTH - 45)}, ${f(JAW_HALF_WIDTH)}, abs(p.x - ${f(HEAD.x)}));
    p.y += uJaw * 22.0 * below * aboveNeck * inJaw; // up to about 4% of the face, as when speaking
    // The head: turns about the neck; the shoulders stay still.
    float onHead = 1.0 - smoothstep(${f(NECK_FROM)}, ${f(NECK_TO)}, p.y);
    vec2 d = (p - vec2(${f(HEAD.x)}, ${f(HEAD.y)})) / vec2(${f(HEAD.rx)}, ${f(HEAD.ry)});
    float nearFront = 1.0 - clamp(length(d), 0.0, 1.0); // the face's middle moves a little more than its edge
    vec2 q = p - vec2(${f(PIVOT.x)}, ${f(PIVOT.y)});
    float a = uHead.z * onHead;
    q = vec2(q.x * cos(a) - q.y * sin(a), q.x * sin(a) + q.y * cos(a));
    q.x += uHead.y * 210.0 * (0.75 + 0.35 * nearFront) * onHead;
    q.y -= uHead.x * 190.0 * onHead;
    q.y *= 1.0 + uHead.x * 0.25 * onHead; // a nod shortens the face a little, as it tips
    p = q + vec2(${f(PIVOT.x)}, ${f(PIVOT.y)});
    // Breathing: the whole figure rises very slightly, from the bottom.
    p.y = ${f(H)} - (${f(H)} - p.y) * uBreath;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p.x - ${f(W / 2)}, ${f(H / 2)} - p.y, 0.0, 1.0);
  }
`;

const FRAGMENT = /* glsl */ `
  uniform sampler2D uMap;
  uniform float uJaw;
  uniform float uEyes;
  varying vec2 vUv;
  varying vec2 vPicture;

  void main() {
    vec4 texel = texture2D(uMap, vUv);
    vec3 color = texel.rgb;
    // The mouth: a little darker along the mouth line as the jaw opens.
    float lip = (1.0 - smoothstep(0.0, 9.0, abs(vPicture.y - ${f(MOUTH.y + 3)})))
              * (1.0 - smoothstep(${f(MOUTH.halfWidth - 20)}, ${f(MOUTH.halfWidth)}, abs(vPicture.x - ${f(HEAD.x)})));
    color *= 1.0 - 0.55 * uJaw * lip;
    // The eyes: a soft green glow that follows the voice.
    float glow = 0.0;
    glow += 1.0 - smoothstep(0.0, ${f(EYE_RADIUS)}, distance(vPicture, vec2(${f(EYES[0][0])}, ${f(EYES[0][1])})));
    glow += 1.0 - smoothstep(0.0, ${f(EYE_RADIUS)}, distance(vPicture, vec2(${f(EYES[1][0])}, ${f(EYES[1][1])})));
    color += vec3(0.31, 1.0, 0.55) * glow * glow * uEyes;
    gl_FragColor = vec4(color, texel.a);
  }
`;

const NOOP = {
  ready: Promise.resolve(false), setState() {}, speakWord() {}, speakWords() {}, closeMouth() {}, setMouthSource() {},
  setListenSource() {}, dispose() {}, jaw: 0, pose: { yaw: 0, pitch: 0, roll: 0, nodding: false },
};

const ease = (value, target, dt, seconds) => value + (target - value) * (1 - Math.exp(-dt / seconds));
const random = (low, high) => low + Math.random() * (high - low);

// Poses (radians) for each state, as for the 3D head: yaw (turn), pitch (down is negative), roll (tilt).
const POSES = {
  idle: { yaw: 0, pitch: 0, roll: 0 },
  speaking: { yaw: 0, pitch: 0, roll: 0 },
  listening: { yaw: 0, pitch: -0.03, roll: 0.008 }, // leaning in slightly
  thinking: { yaw: -0.06, pitch: 0.025, roll: -0.006 }, // glancing up and away
};
const EYE_GLOW = { idle: 0.05, speaking: 0.1, listening: 0.15, thinking: 0.02 };

// states: "idle" | "speaking" | "listening" | "thinking"
export function createAvatar(container) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch {
    return NOOP; // no WebGL: the consultation still works, just without the avatar
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0); // transparent: the page's matrix rain shows all around the android
  container.append(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-W / 2, W / 2, H / 2, -H / 2, -10, 10);
  const uniforms = {
    uMap: { value: null }, uJaw: { value: 0 }, uHead: { value: new THREE.Vector3() }, uBreath: { value: 1 },
    uEyes: { value: 0.05 },
  };
  const material = new THREE.ShaderMaterial({ uniforms, vertexShader: VERTEX, fragmentShader: FRAGMENT, transparent: true });
  const geometry = new THREE.PlaneGeometry(W, H, 128, 96); // fine enough for smooth bending
  const mesh = new THREE.Mesh(geometry, material);
  mesh.visible = false; // until the picture has loaded
  scene.add(mesh);
  const ready = new THREE.TextureLoader().loadAsync(IMAGE_URL).then((texture) => {
    texture.minFilter = THREE.LinearMipmapLinearFilter;
    texture.anisotropy = renderer.capabilities.getMaxAnisotropy();
    uniforms.uMap.value = texture;
    mesh.visible = true;
    return true;
  }).catch(() => false);

  let state = "idle";
  let jaw = 0;
  let mouth = []; // scheduled syllables (device voice): { start, end, peak } in ms
  let mouthSource = null; // () => 0..1, the loudness of the practice voice as it plays
  let listenSource = null; // () => dB, the client's microphone level
  const pose = { yaw: 0, pitch: 0, roll: 0 };
  const phrase = { yaw: 0, roll: 0 }; // the slight shift a speaker makes from one phrase to the next
  let nods = []; // { start, duration, amount }: added to the pitch
  let lastNod = 0;
  let stressReady = true;
  let voiceQuietSince = 0;
  const listening = { floor: -60, talkingMs: 0, quietMs: 0 };
  let eyes = 0.05;
  let previous = performance.now();
  let frame = 0;
  let disposed = false;

  // The whole bust always fits (shoulders complete), standing on the bottom of the area, centred on the face.
  function resize() {
    const { width, height } = container.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    const scale = Math.min(width / W, height / H); // screen pixels per picture pixel
    const halfWidth = width / scale / 2;
    const viewHeight = height / scale;
    camera.left = HEAD.x - W / 2 - halfWidth;
    camera.right = HEAD.x - W / 2 + halfWidth;
    camera.bottom = -H / 2;
    camera.top = -H / 2 + viewHeight;
    camera.updateProjectionMatrix();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  const nod = (now, amount, duration) => {
    nods.push({ start: now, duration, amount });
    lastNod = now;
  };

  function scheduleWord(word, rate, at) {
    const count = syllables(word);
    const step = SYLLABLE_MS / Math.max(rate, 0.3);
    for (let i = 0; i < count; i += 1) {
      mouth.push({ start: at + i * step, end: at + (i + 0.85) * step, peak: 0.55 + Math.random() * 0.45 });
    }
    return at + count * step;
  }

  // While speaking, the head follows the voice: a small nod on a stressed syllable, a slight change of position
  // when a new phrase starts after a pause, and stillness in the pauses.
  function followOwnVoice(now, level) {
    if (level < 0.4) stressReady = true;
    if (level > 0.75 && stressReady) {
      stressReady = false;
      if (now - lastNod > 450 && Math.random() < 0.35) nod(now, random(0.015, 0.028), 280);
    }
    if (level > 0.3) {
      if (voiceQuietSince && now - voiceQuietSince > 350) {
        phrase.yaw = random(-0.03, 0.03);
        phrase.roll = random(-0.012, 0.012);
      }
      voiceQuietSince = 0;
    } else if (!voiceQuietSince) {
      voiceQuietSince = now;
    }
    if (voiceQuietSince && now - voiceQuietSince > 1200) {
      phrase.yaw = 0;
      phrase.roll = 0;
    }
  }

  // While listening, a nod when the client finishes a phrase and pauses, as a listener does ("mm-hm").
  function followClient(now, dt, db) {
    listening.floor = db < listening.floor ? db : listening.floor + dt * 0.003;
    if (db > listening.floor + 12) {
      listening.talkingMs += dt;
      listening.quietMs = 0;
    } else {
      listening.quietMs += dt;
      if (listening.talkingMs > 700 && listening.quietMs > 300) {
        if (now - lastNod > 2500 && Math.random() < 0.7) nod(now, random(0.035, 0.05), 650);
        listening.talkingMs = 0;
      }
      if (listening.quietMs > 2000) listening.talkingMs = 0;
    }
  }

  function animate(now) {
    if (disposed) return;
    frame = requestAnimationFrame(animate);
    const dt = Math.min(100, now - previous);
    previous = now;

    mouth = mouth.filter((s) => s.end > now);
    let target = 0;
    for (const s of mouth) {
      if (s.start <= now) target = Math.max(target, s.peak * Math.sin(Math.PI * (now - s.start) / (s.end - s.start)));
    }
    if (mouthSource) target = Math.max(target, mouthSource()); // silent between words, so it closes in pauses
    jaw += (target - jaw) * (target > jaw ? 0.55 : 0.35);
    if (state === "speaking") followOwnVoice(now, jaw);
    if (state === "listening" && listenSource) followClient(now, dt, listenSource());

    nods = nods.filter((n) => now - n.start < n.duration);
    const nodPitch = nods.reduce((sum, n) => sum + n.amount * Math.sin(Math.PI * ((now - n.start) / n.duration)), 0);
    const base = POSES[state] || POSES.idle;
    pose.yaw = ease(pose.yaw, base.yaw + (state === "speaking" ? phrase.yaw : 0), dt, 450);
    pose.roll = ease(pose.roll, base.roll + (state === "speaking" ? phrase.roll : 0), dt, 600);
    pose.pitch = ease(pose.pitch, base.pitch, dt, 450);
    eyes = ease(eyes, EYE_GLOW[state] ?? 0.05, dt, 350);

    uniforms.uJaw.value = jaw;
    uniforms.uHead.value.set(pose.pitch - nodPitch, pose.yaw, pose.roll);
    uniforms.uBreath.value = 1 + Math.sin(now / 1000 * 1.2) * 0.003;
    uniforms.uEyes.value = eyes + (state === "speaking" ? jaw * 0.35 : 0);
    renderer.render(scene, camera);
  }
  frame = requestAnimationFrame(animate);

  return {
    ready,
    setState(next) {
      state = next;
      listening.talkingMs = 0;
      if (next !== "speaking") mouth = [];
    },
    speakWord(word, rate = 1) {
      const now = performance.now();
      mouth = mouth.filter((s) => s.start < now);
      scheduleWord(word, rate, now);
    },
    speakWords(words, rate = 1, startedAt = performance.now()) {
      let at = startedAt;
      for (const word of words) at = scheduleWord(word, rate, at) + 60 / Math.max(rate, 0.3);
    },
    closeMouth() { mouth = []; },
    setMouthSource(source) { mouthSource = source; },
    setListenSource(source) { listenSource = source; },
    get jaw() { return jaw; },
    get pose() { return { ...pose, nodding: nods.length > 0 }; },
    // For checking: sets the head and jaw directly (radians, 0..1), then draws.
    debugPose(pitch, yaw, roll, jawOpen) {
      uniforms.uHead.value.set(pitch, yaw, roll);
      uniforms.uJaw.value = jawOpen;
      renderer.render(scene, camera);
    },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      uniforms.uMap.value?.dispose();
      material.dispose();
      geometry.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
