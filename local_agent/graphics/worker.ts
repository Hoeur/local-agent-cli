// Off-screen worker: stdin/stdout are JSONL, never the user's terminal.
import { createInterface } from "node:readline";
import { createTestRenderer } from "@opentui/core/testing";
import { OptimizedBuffer, RGBA } from "@opentui/core";
import { ThreeCliRenderer } from "@opentui/three";
import * as THREE from "three";

const output = (value: unknown) => process.stdout.write(JSON.stringify(value) + "\n");
// Keep dependency diagnostics out of the frame channel.
console.log = console.warn = console.error = (...args: unknown[]) => {
  if (process.env.HOLOGRAM_DEBUG) process.stderr.write(args.map(String).join(" ") + "\n");
};
let test: Awaited<ReturnType<typeof createTestRenderer>> | undefined;
let engine: ThreeCliRenderer | undefined;
let buffer: OptimizedBuffer | undefined;
const scene = new THREE.Scene();
const resources: Array<{ dispose(): void }> = [];
const material = (color: number, opacity = 1) => {
  const result = new THREE.MeshBasicMaterial({ color, transparent: true, opacity, wireframe: true });
  resources.push(result);
  return result;
};
function mesh(geometry: THREE.BufferGeometry, mat: THREE.MeshBasicMaterial) {
  resources.push(geometry);
  const result = new THREE.Mesh(geometry, mat);
  scene.add(result);
  return result;
}
const shell = mesh(new THREE.SphereGeometry(1, 24, 12), material(0x38bbff, 0.55));
const core = mesh(new THREE.IcosahedronGeometry(0.18, 1), material(0xaaffff));
core.material.wireframe = false;
const rings = [0, 1, 2].map(i => mesh(new THREE.TorusGeometry(1.23 + i * 0.15, 0.008, 3, 100), material(0x30dcff, 0.8)));
const wave = mesh(new THREE.TorusGeometry(1, 0.012, 3, 100), material(0x50dfff, 0.6));
const positions = new Float32Array(64 * 3);
const geometry = new THREE.BufferGeometry();
geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
const particlesMaterial = new THREE.PointsMaterial({ color: 0x80faff, size: 0.035 });
const particles = new THREE.Points(geometry, particlesMaterial);
resources.push(geometry, particlesMaterial);
scene.add(particles);
const camera = new THREE.PerspectiveCamera(48, 1, 0.1, 100);
camera.position.z = 5.2;
camera.lookAt(0, 0, 0);

try {
  test = await createTestRenderer({ width: 40, height: 16, useMouse: false, exitOnCtrlC: false });
  engine = new ThreeCliRenderer(test.renderer, { width: 40, height: 16, autoResize: false,
    backgroundColor: RGBA.fromHex("#080f18") });
  await engine.init(); // A real adapter/device probe; import success is insufficient.
  output({ ready: true });
  for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
    const frame = JSON.parse(line);
    const width = Math.max(1, Math.min(120, frame.width | 0));
    const height = Math.max(1, Math.min(40, frame.height | 0));
    if (!buffer || buffer.width !== width || buffer.height !== height) {
      buffer?.destroy();
      buffer = OptimizedBuffer.create(width, height, "unicode", { owner: test.renderer.nativeScene });
      engine.setSize(width, height);
      camera.aspect = width / (height * 2);
      camera.updateProjectionMatrix();
    }
    const p = frame.phase;
    shell.rotation.set(frame.pitch, p + frame.yaw, 0);
    shell.material.opacity = 0.35 + frame.energy * 0.3;
    core.scale.setScalar(1 + frame.energy * 0.5 + Math.sin(p * 4) * 0.15);
    core.material.color.setRGB(...frame.color.map((c: number) => c / 255) as [number, number, number]);
    rings.forEach((ring, i) => ring.rotation.set(0.4 + i * 0.75 + p * (i - 1) * 0.3, p * (0.4 + i * 0.3) + frame.yaw, i));
    wave.visible = ["generating", "success", "error"].includes(frame.state);
    wave.scale.setScalar(0.25 + (frame.state === "generating" ? p * 1.8 % 1.5 : frame.pulse * 1.7));
    wave.material.color.copy(core.material.color);
    for (let i = 0; i < 64; i++) {
      const a = i * 2.39996 + p * (1 + i % 3 * 0.4);
      const r = frame.state === "thinking" ? 1.6 - 0.7 * (0.5 + Math.sin(p * 3 + i) * 0.5) : 1.6;
      positions[i * 3] = Math.cos(a) * r;
      positions[i * 3 + 1] = Math.sin(a * 2 + i) * (0.5 + frame.audio * 0.4);
      positions[i * 3 + 2] = Math.sin(a) * r;
    }
    geometry.attributes.position.needsUpdate = true;
    buffer.clear(RGBA.fromHex("#080f18"));
    await engine.doDrawScene(scene, camera, buffer, 1 / 30);
    const lines = buffer.getSpanLines();
    // Some drivers create a device but silently fail every draw. A core frame
    // always contains colored pixels; reject blank readbacks instead of hiding it.
    if (!lines.some(line => line.spans.some(span => span.text.trim() ||
        span.bg.toInts().slice(0, 3).some((c, i) => Math.abs(c - [8, 15, 24][i]) > 8)))) {
      throw new Error("WebGPU returned an empty frame; driver rendering is unavailable");
    }
    output({ lines: lines.map(line => line.spans.map(span => ({
      text: span.text, fg: span.fg.toInts().slice(0, 3), bg: span.bg.toInts().slice(0, 3),
    }))) });
  }
} catch (error) {
  output({ error: String(error).slice(0, 240) });
  process.exitCode = 1;
} finally {
  resources.forEach(resource => resource.dispose());
  engine?.destroy();
  buffer?.destroy();
  await test?.dispose();
}
