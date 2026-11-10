// The consultant avatar: a smooth green head rendered with three.js, lit naturally, with green eyes.
// Geometry: the "Infinite" 3D head scan by Lee Perry-Smith, CC BY 3.0 (see vendor/head/LeePerrySmith_License.txt),
// shown smooth (17,684 triangles with the scan's own rounded normals), so the skull is oval, with no angles.
//
// It behaves like a person and moves only for a reason: still while at rest (just breathing); while speaking its
// head follows its own voice (a small nod on a stressed syllable, a slight shift between phrases, still in the
// pauses); while listening it settles into an attentive pose and nods when the client finishes a phrase; while
// thinking it glances away once and holds. Nothing moves in a repeating loop. The mouth opens only with the voice.

import * as THREE from "./vendor/three/three.module.js";

const MODEL_URL = "/static/vendor/head/LeePerrySmith.glb";
const SKIN = new THREE.Color(0x2fc877); // a natural green
const IRIS = new THREE.Color(0x1fd862); // clearly green (not pale, so it never looks white)
const PUPIL = new THREE.Color(0x03180c);
// Measured on the scan (its own units): the face's centre line, and the middle of each eye. The scan's eyes are
// closed, so the green irises sit on the lids exactly where the eyes are, level with each other.
const FACE_X = -0.109;
const EYE_CENTRES = [{ x: -0.722, y: 1.645 }, { x: 0.503, y: 1.645 }];
const EYE_RADIUS = 0.12;
const SYLLABLE_MS = 175; // one syllable at speech rate 1.0

export function syllables(word) {
  const letters = word.toLowerCase().replace(/[^a-z]/g, "");
  if (!letters) return 0;
  const groups = letters.replace(/(?:[^laeiouy]es|ed|[^laeiouy]e)$/, "").match(/[aeiouy]+/g);
  return Math.max(1, groups ? groups.length : 1);
}

// The jaw hinges open, and the head turns on the neck while the shoulders stay still.
const FACE_VERTEX = /* glsl */ `
  uniform float uJaw;
  uniform vec3 uPivot;
  uniform float uMouthY;
  uniform float uChinY;
  uniform float uJawHalfWidth;
  uniform float uFrontZ;
  uniform mat3 uHeadRotation;
  uniform vec3 uNeck;
  uniform float uNeckLow;
  uniform float uNeckHigh;
  varying vec3 vNormal;
  varying vec3 vViewPosition;

  vec3 rotateX(vec3 p, float a) {
    float c = cos(a), s = sin(a);
    return vec3(p.x, p.y * c - p.z * s, p.y * s + p.z * c);
  }

  float jawAngle(vec3 p) {
    float belowLips = smoothstep(uMouthY, uMouthY - 0.08, p.y);
    float aboveNeck = smoothstep(uChinY - 0.45, uChinY + 0.05, p.y);
    float front = smoothstep(uFrontZ - 0.6, uFrontZ, p.z);
    float inside = 1.0 - smoothstep(uJawHalfWidth * 0.7, uJawHalfWidth, abs(p.x));
    return uJaw * 0.28 * belowLips * aboveNeck * front * inside;
  }

  void main() {
    float onHead = smoothstep(uNeckLow, uNeckHigh, position.y);
    vec3 q = rotateX(position - uPivot, jawAngle(position)) + uPivot;
    vec3 p = mix(q, uHeadRotation * (q - uNeck) + uNeck, onHead);
    vec3 n = rotateX(normal, jawAngle(position));
    n = normalize(mix(n, uHeadRotation * n, onHead));
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vViewPosition = mv.xyz;
    vNormal = normalize(normalMatrix * n);
    gl_Position = projectionMatrix * mv;
  }
`;

// Soft studio lighting on solid green facets: a key light, a fill, a gentle rim and a faint sheen.
const FACE_FRAGMENT = /* glsl */ `
  uniform vec3 uColor;
  uniform float uGlow;
  varying vec3 vNormal;
  varying vec3 vViewPosition;
  void main() {
    vec3 n = normalize(vNormal);
    vec3 v = normalize(-vViewPosition);
    vec3 keyDir = normalize(vec3(-0.4, 0.55, 0.75));
    float key = max(dot(n, keyDir), 0.0);
    float fill = max(dot(n, normalize(vec3(0.65, 0.05, 0.6))), 0.0);
    float rim = pow(1.0 - max(dot(n, v), 0.0), 2.5);
    float sheen = pow(max(dot(n, normalize(keyDir + v)), 0.0), 24.0);
    vec3 color = uColor * (0.12 + 0.68 * key + 0.22 * fill + 0.3 * rim) + vec3(0.7, 1.0, 0.8) * sheen * 0.15;
    gl_FragColor = vec4(color * (0.88 + 0.2 * uGlow), 1.0);
  }
`;

function glowSprite(color, strength) {
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  const [r, g, b] = [color.r, color.g, color.b].map((c) => Math.round(c * 255));
  gradient.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${strength})`);
  gradient.addColorStop(0.45, `rgba(${r}, ${g}, ${b}, ${strength * 0.2})`);
  gradient.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  return new THREE.Sprite(new THREE.SpriteMaterial({
    map: new THREE.CanvasTexture(canvas), blending: THREE.AdditiveBlending, depthWrite: false, transparent: true,
  }));
}

// A green iris like a real one: brighter green near the pupil, deeper green at the edge, and a dark ring around it.
function irisTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 128;
  canvas.height = 128;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  gradient.addColorStop(0, "#30f070");
  gradient.addColorStop(0.55, "#1fd862");
  gradient.addColorStop(0.85, "#0e9c40");
  gradient.addColorStop(0.94, "#064a1f");
  gradient.addColorStop(1, "#043315");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 128, 128);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

// A human-like eye: a green iris with a dark pupil that looks around, and a soft green glow. Blinks by closing
// vertically.
function createEye(radius) {
  const group = new THREE.Group();
  const iris = new THREE.Mesh(new THREE.CircleGeometry(radius, 48), new THREE.MeshBasicMaterial({ map: irisTexture() }));
  const pupil = new THREE.Mesh(new THREE.CircleGeometry(radius * 0.4, 32), new THREE.MeshBasicMaterial({ color: PUPIL }));
  pupil.position.z = 0.002;
  const glow = glowSprite(IRIS, 0.3);
  glow.scale.setScalar(radius * 2.8);
  glow.position.z = -0.001;
  group.add(glow, iris, pupil);
  return { group, pupil, parts: [iris, pupil, glow] };
}

// The head scan (a binary glTF file): its positions, smooth normals and triangles.
async function loadHead(url) {
  const data = await (await fetch(url)).arrayBuffer();
  const view = new DataView(data);
  const jsonLength = view.getUint32(12, true);
  const gltf = JSON.parse(new TextDecoder().decode(new Uint8Array(data, 20, jsonLength)));
  const binaryStart = 20 + jsonLength + 8;
  const read = (index) => {
    const accessor = gltf.accessors[index];
    const bufferView = gltf.bufferViews[accessor.bufferView];
    const Type = { 5126: Float32Array, 5123: Uint16Array, 5125: Uint32Array }[accessor.componentType];
    const size = { SCALAR: 1, VEC2: 2, VEC3: 3 }[accessor.type];
    const start = binaryStart + (bufferView.byteOffset || 0) + (accessor.byteOffset || 0);
    return new Type(data.slice(start, start + accessor.count * size * Type.BYTES_PER_ELEMENT));
  };
  const primitive = gltf.meshes[0].primitives[0];
  return { positions: read(primitive.attributes.POSITION), normals: read(primitive.attributes.NORMAL),
           indices: read(primitive.indices) };
}

const NOOP = {
  ready: Promise.resolve(false), setState() {}, speakWord() {}, speakWords() {}, closeMouth() {}, setMouthSource() {},
  setListenSource() {}, dispose() {}, jaw: 0,
};

// Eases a value towards a target over roughly `seconds`, whatever the frame rate.
const ease = (value, target, dt, seconds) => value + (target - value) * (1 - Math.exp(-dt / seconds));
const random = (low, high) => low + Math.random() * (high - low);

// Poses (radians) for each state: yaw (turn), pitch (down is negative), roll (tilt).
const POSES = {
  idle: { yaw: 0, pitch: -0.02, roll: 0 },
  speaking: { yaw: 0, pitch: -0.02, roll: 0 },
  listening: { yaw: 0, pitch: -0.035, roll: 0.008 }, // leaning in slightly (hardly any tilt, so the eyes stay level)
  thinking: { yaw: -0.07, pitch: 0.03, roll: -0.006 }, // glancing up and away
};

// states: "idle" | "speaking" | "listening" | "thinking"
export function createAvatar(container) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  } catch {
    return NOOP; // no WebGL: the consultation still works, just without the head
  }
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  renderer.setPixelRatio(pixelRatio);
  renderer.setClearColor(0x000000, 0);
  container.append(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 200);
  const body = new THREE.Group(); // breathing; the head turns on the neck inside the shader
  scene.add(body);
  const eyeRig = new THREE.Group(); // follows the head's turn, around the same neck pivot
  const headEuler = new THREE.Euler(0, 0, 0, "YXZ");
  const headMatrix = new THREE.Matrix4();
  const halo = glowSprite(SKIN, 0.35);
  scene.add(halo);

  const uniforms = {
    uColor: { value: SKIN },
    uGlow: { value: 0 },
    uJaw: { value: 0 },
    uPivot: { value: new THREE.Vector3() },
    uMouthY: { value: 0 },
    uChinY: { value: 0 },
    uJawHalfWidth: { value: 1 },
    uFrontZ: { value: 0 },
    uHeadRotation: { value: new THREE.Matrix3() },
    uNeck: { value: new THREE.Vector3() },
    uNeckLow: { value: 0 },
    uNeckHigh: { value: 0 },
  };
  const faceMaterial = new THREE.ShaderMaterial({ uniforms, vertexShader: FACE_VERTEX, fragmentShader: FACE_FRAGMENT });
  const eyes = [createEye(EYE_RADIUS), createEye(EYE_RADIUS)];
  const geometries = [];

  let state = "idle";
  let jaw = 0;
  let glow = 0.35;
  let mouth = []; // scheduled syllables (device voice): { start, end, peak } in ms
  let mouthSource = null; // () => 0..1, the loudness of the practice voice as it plays
  let listenSource = null; // () => dB, the client's microphone level
  const pose = { yaw: 0, pitch: -0.02, roll: 0 };
  const phrase = { yaw: 0, roll: 0 }; // the slight shift a speaker makes from one phrase to the next
  let nods = []; // { start, duration, amount }: added to the pitch
  let lastNod = 0;
  let stressReady = true; // the voice dropped between syllables, so the next strong one can bring a nod
  let voiceQuietSince = 0;
  const listening = { floor: -60, talkingMs: 0, quietMs: 0 };
  let nextBlink = performance.now() + 1500;
  let blinkStart = -1;
  const gaze = new THREE.Vector2();
  const gazeTarget = new THREE.Vector2();
  let nextGaze = 0;
  let previous = performance.now();
  let frame = 0;
  let disposed = false;

  const ready = loadHead(MODEL_URL).then(({ positions, normals, indices }) => {
    if (disposed) return false;
    const head = new THREE.BufferGeometry();
    head.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    head.setAttribute("normal", new THREE.BufferAttribute(normals, 3)); // the scan's smooth, rounded surface
    head.setIndex(new THREE.BufferAttribute(indices, 1));
    head.computeBoundingBox();
    const box = head.boundingBox;
    const size = box.getSize(new THREE.Vector3());
    const centre = box.getCenter(new THREE.Vector3()).setX(FACE_X); // centred on the face, not the bounding box
    geometries.push(head);

    // Lip line, chin and jaw hinge as proportions of the scanned bust (head and shoulders).
    uniforms.uMouthY.value = box.min.y + size.y * 0.565;
    uniforms.uChinY.value = box.min.y + size.y * 0.45;
    uniforms.uPivot.value.set(0, box.min.y + size.y * 0.63, centre.z);
    uniforms.uJawHalfWidth.value = size.x * 0.13;
    uniforms.uFrontZ.value = box.max.z - size.z * 0.25;
    const neck = new THREE.Vector3(FACE_X, box.min.y + size.y * 0.38, centre.z); // the head turns about its middle
    uniforms.uNeck.value.copy(neck);
    uniforms.uNeckLow.value = box.min.y + size.y * 0.32;
    uniforms.uNeckHigh.value = box.min.y + size.y * 0.42;

    const model = new THREE.Group();
    const face = new THREE.Mesh(head, faceMaterial);
    model.add(face);
    // Each eye sits just in front of the eyelid, at the point measured on the scan (found by a ray from the front).
    eyeRig.position.copy(neck);
    const ray = new THREE.Raycaster();
    const probe = new THREE.Mesh(head, new THREE.MeshBasicMaterial({ side: THREE.DoubleSide }));
    probe.updateMatrixWorld();
    eyes.forEach((eye, i) => {
      const { x, y } = EYE_CENTRES[i];
      ray.set(new THREE.Vector3(x, y, box.max.z + 1), new THREE.Vector3(0, 0, -1));
      const hit = ray.intersectObject(probe)[0];
      const z = hit ? hit.point.z : box.max.z;
      eye.group.position.set(x - neck.x, y - neck.y, z + 0.025 - neck.z);
      eyeRig.add(eye.group);
    });
    probe.material.dispose();
    model.add(eyeRig);
    model.position.sub(centre);
    body.add(model);
    // Video-call framing: head and upper shoulders.
    camera.position.set(0, size.y * 0.2, size.y * 1.75);
    camera.lookAt(0, size.y * 0.13, 0);
    halo.scale.set(size.y * 1.25, size.y * 1.25, 1); // fades out inside the frame, so no visible edge
    halo.position.set(0, size.y * 0.22, -size.z);
    return true;
  }).catch(() => false);

  function resize() {
    const { width, height } = container.getBoundingClientRect();
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(container);
  resize();

  const nod = (now, amount, duration) => {
    nods.push({ start: now, duration, amount });
    lastNod = now;
  };
  const blink = (now) => { if (blinkStart < 0) blinkStart = now; };

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
    if (level < 0.4) stressReady = true; // between syllables
    if (level > 0.75 && stressReady) { // a strong syllable
      stressReady = false;
      if (now - lastNod > 450 && Math.random() < 0.35) nod(now, random(0.015, 0.028), 280);
    }
    if (level > 0.3) {
      if (voiceQuietSince && now - voiceQuietSince > 350) {
        phrase.yaw = random(-0.035, 0.035);
        phrase.roll = random(-0.015, 0.015);
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
    listening.floor = db < listening.floor ? db : listening.floor + dt * 0.003; // the room's quiet level
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
    const t = now / 1000;

    // Mouth: open only while the voice is sounding.
    mouth = mouth.filter((s) => s.end > now);
    let target = 0;
    for (const s of mouth) {
      if (s.start <= now) target = Math.max(target, s.peak * Math.sin(Math.PI * (now - s.start) / (s.end - s.start)));
    }
    if (mouthSource) target = Math.max(target, mouthSource()); // silent between words, so it closes in pauses
    jaw += (target - jaw) * (target > jaw ? 0.55 : 0.35);

    if (state === "speaking") followOwnVoice(now, jaw);
    if (state === "listening" && listenSource) followClient(now, dt, listenSource());

    // Blinks: every 2.5-6 s, sometimes twice.
    if (now > nextBlink) blink(now);
    let lid = 1;
    if (blinkStart >= 0) {
      const p = (now - blinkStart) / 190;
      lid = p < 0.4 ? 1 - p / 0.4 : Math.min(1, (p - 0.4) / 0.6);
      if (p >= 1) {
        blinkStart = -1;
        nextBlink = now + (Math.random() < 0.15 ? 220 : 2500 + Math.random() * 3500);
      }
    }

    // Gaze: on the person, with small natural shifts; away while thinking (held, not wandering).
    if (now > nextGaze) {
      if (state === "thinking") gazeTarget.set(-0.45, 0.35);
      else gazeTarget.set(random(-0.12, 0.12), random(-0.08, 0.08));
      nextGaze = now + (state === "thinking" ? 60000 : random(1200, 4000));
    }
    gaze.x = ease(gaze.x, gazeTarget.x, dt, 60);
    gaze.y = ease(gaze.y, gazeTarget.y, dt, 60);
    eyes.forEach((eye) => {
      eye.group.scale.y = Math.max(lid, 0.06);
      eye.pupil.position.x = gaze.x * 0.06;
      eye.pupil.position.y = gaze.y * 0.06;
    });

    // Head: eases into the pose of the moment; nods are added on top. No motion without a reason.
    nods = nods.filter((n) => now - n.start < n.duration);
    const nodPitch = nods.reduce((sum, n) => sum + n.amount * Math.sin(Math.PI * ((now - n.start) / n.duration)), 0);
    const base = POSES[state] || POSES.idle;
    pose.yaw = ease(pose.yaw, base.yaw + (state === "speaking" ? phrase.yaw : 0), dt, 450);
    pose.roll = ease(pose.roll, base.roll + (state === "speaking" ? phrase.roll : 0), dt, 600);
    pose.pitch = ease(pose.pitch, base.pitch, dt, 450);
    headEuler.set(pose.pitch - nodPitch, pose.yaw, pose.roll);
    headMatrix.makeRotationFromEuler(headEuler);
    uniforms.uHeadRotation.value.setFromMatrix4(headMatrix);
    eyeRig.rotation.copy(headEuler);
    body.scale.y = 1 + Math.sin(t * 1.3) * 0.003; // breathing

    const glowTarget = { speaking: 0.9, listening: 0.6, thinking: 0.45 }[state] ?? 0.35;
    glow = ease(glow, glowTarget, dt, 400);
    halo.material.opacity = 0.35 + 0.5 * glow;
    uniforms.uJaw.value = jaw;
    uniforms.uGlow.value = glow;
    renderer.render(scene, camera);
  }
  frame = requestAnimationFrame(animate);

  return {
    ready,
    setState(next) {
      if (next !== state) blink(performance.now()); // people blink when their attention shifts
      state = next;
      nextGaze = 0;
      listening.talkingMs = 0;
      if (next !== "speaking") mouth = [];
    },
    // A word the voice has just started saying (from the speech engine's word events).
    speakWord(word, rate = 1) {
      const now = performance.now();
      mouth = mouth.filter((s) => s.start < now);
      scheduleWord(word, rate, now);
    },
    // Fallback for voices without word events: spread a sentence's words over its estimated duration.
    speakWords(words, rate = 1, startedAt = performance.now()) {
      let at = startedAt;
      for (const word of words) at = scheduleWord(word, rate, at) + 60 / Math.max(rate, 0.3);
    },
    closeMouth() { mouth = []; },
    // Opens the mouth with the loudness of the voice as it plays, for voices without word events.
    setMouthSource(source) { mouthSource = source; },
    // The client's microphone level (dB), so the avatar can nod when they finish a phrase.
    setListenSource(source) { listenSource = source; },
    get jaw() { return jaw; },
    get pose() { return { ...pose, nodding: nods.length > 0 }; },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      faceMaterial.dispose();
      halo.material.map.dispose();
      halo.material.dispose();
      eyes.forEach((eye) => eye.parts.forEach((part) => {
        part.geometry?.dispose();
        part.material.map?.dispose();
        part.material.dispose();
      }));
      geometries.forEach((g) => g.dispose());
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
