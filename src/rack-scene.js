import {
  ACESFilmicToneMapping, AmbientLight, BoxGeometry, Color, DirectionalLight, Fog, GridHelper,
  Group, HemisphereLight, Mesh, MeshBasicMaterial, MeshStandardMaterial, PerspectiveCamera, PlaneGeometry,
  Raycaster, Scene, SRGBColorSpace, Vector2, Vector3, WebGLRenderer,
} from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
export { recordWalkthrough } from '../public/rack-recording.js';
export { diagnoseGpu } from './rack-health.js';

const palette = {
  clear: 0x8accab,
  warning: 0xffae57,
  fault: 0xff4c42,
  unknown: 0x879590,
};
const visualState = (value) => Object.hasOwn(palette, value) ? value : 'unknown';
const cameraUp = new Vector3();
const projectPoint = new Vector3();

function surface(color, metalness = 0.15, roughness = 0.52) {
  return new MeshStandardMaterial({ color, metalness, roughness });
}

function box(parent, material, dimensions, point) {
  const mesh = new Mesh(new BoxGeometry(...dimensions), material);
  mesh.position.set(...point);
  parent.add(mesh);
  return mesh;
}

function mergeStatic(objects, parent) {
  const parts = new Map();
  for (const object of objects) {
    object.updateMatrix();
    const transformed = object.geometry.clone().applyMatrix4(object.matrix);
    const list = parts.get(object.material) ?? [];
    list.push(transformed);
    parts.set(object.material, list);
  }
  for (const [material, geometries] of parts) {
    parent.add(new Mesh(mergeGeometries(geometries), material));
    geometries.forEach((geometry) => geometry.dispose());
  }
}

export async function mountRackScene(container, racks, onSelect, onHover) {
  const model = await new GLTFLoader().loadAsync('./assets/gpu-rack.glb');
  const originals = model.scene.children.filter((child) => child.isMesh);
  if (originals.filter((object) => /^GPU tray \d\d$/.test(object.userData.name ?? object.name)).length !== 8) {
    throw new Error('The rack model is missing its named trays.');
  }

  const renderer = new WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.3;
  renderer.domElement.setAttribute('aria-label', '3D GPU racks. Use the GPU selector for keyboard access.');
  container.prepend(renderer.domElement);
  const stage = new Scene();
  stage.background = new Color('#1e3028');
  stage.fog = new Fog('#1e3028', 8, 21);
  stage.add(new AmbientLight('#dcf1e2', 0.65));
  stage.add(new HemisphereLight('#e5f1e1', '#46594e', 2.0));
  const key = new DirectionalLight('#f6f2e5', 3);
  key.position.set(-4, 6, 5);
  stage.add(key);
  const fill = new DirectionalLight('#a8cdbc', 1.6);
  fill.position.set(5, 3, -3);
  stage.add(fill);
  const floor = new Mesh(new PlaneGeometry(80, 80), surface('#20362b', 0.03, 0.89));
  floor.rotateX(-Math.PI / 2);
  floor.position.y = -.012;
  stage.add(floor);
  const grid = new GridHelper(22, 44, '#536a5b', '#3e5549');
  grid.position.y = -.008;
  grid.material.transparent = true;
  grid.material.opacity = .48;
  stage.add(grid);

  const camera = new PerspectiveCamera(34, 1, .08, 42);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = .09;
  controls.enablePan = false;
  controls.minAzimuthAngle = -.70;
  controls.maxAzimuthAngle = .73;
  controls.minPolarAngle = .68;
  controls.maxPolarAngle = 1.53;
  controls.minDistance = 2.75;
  controls.maxDistance = 13;

  const materials = new Map();
  for (const object of originals) {
    if (materials.has(object.material)) continue;
    const mat = object.material.clone();
    mat.metalness *= .33;
    mat.roughness = Math.max(.37, mat.roughness);
    materials.set(object.material, mat);
  }
  const baseLamp = surface('#7fac94', .08, .37);
  const faultOutline = new MeshBasicMaterial({ color: '#ff6155', toneMapped: false });
  const faultHorizontal = new BoxGeometry(.468, .008, .011);
  const faultVertical = new BoxGeometry(.008, .119, .011);
  const drawerBed = surface('#546761', .24, .39);
  const board = surface('#265044', .07, .52);
  const chip = surface('#172b27', .39, .35);
  const chipPlate = surface('#82948c', .25, .46);
  const socket = surface('#586b60', .1, .46);
  const trays = new Map();
  const rackRoots = new Map();
  const rackLights = new Map();
  const hitFaces = [];

  for (const [i, rack] of racks.entries()) {
    const rackRoot = new Group();
    rackRoot.position.x = (i - 1) * 1.17;
    rackRoots.set(rack.id, rackRoot);
    stage.add(rackRoot);
    const drawerParents = rack.gpus.map(() => new Group());
    drawerParents.forEach((drawer) => rackRoot.add(drawer));
    const shell = [];
    const moving = drawerParents.map(() => []);
    for (const original of originals) {
      const name = original.userData.name ?? original.name;
      if (name === 'A02 rack identifier' || name === 'A02 uplink lamp' || name.includes('status lamp')) continue;
      const duplicate = original.clone();
      duplicate.material = materials.get(original.material);
      const index = name.match(/^(?:GPU tray|Tray) (\d\d)(?: |$)/);
      if (index) moving[Number(index[1]) - 1].push(duplicate);
      else shell.push(duplicate);
    }
    mergeStatic(shell, rackRoot);
    const lamp = baseLamp.clone();
    box(rackRoot, lamp, [.019, .013, .01], [.211, 1.828, .501]);
    rackLights.set(rack.id, lamp);

    for (const [index, gpu] of rack.gpus.entries()) {
      const drawer = drawerParents[index];
      const y = .44 + index * .165;
      mergeStatic(moving[index], drawer);
      const badge = baseLamp.clone();
      box(drawer, badge, [.014, .010, .008], [.157, y + .019, .490]);
      const face = box(drawer, new MeshStandardMaterial({
        color: palette.clear, transparent: true, opacity: 0, depthWrite: false,
      }), [.459, .132, .011], [0, y, .507]);
      face.userData.gpuId = gpu.gpu_id;
      hitFaces.push(face);
      const marker = new Group();
      const outlineParts = [
        [faultHorizontal, 0, -.063], [faultHorizontal, 0, .063],
        [faultVertical, -.23, 0], [faultVertical, .23, 0],
      ];
      for (const [geometry, x, offset] of outlineParts) {
        const edge = new Mesh(geometry, faultOutline);
        edge.position.set(x, y + offset, .518);
        marker.add(edge);
      }
      marker.visible = false;
      drawer.add(marker);
      const hardware = new Group();
      drawer.add(hardware);
      box(hardware, drawerBed, [.453, .020, .58], [0, y - .051, .122]);
      box(hardware, board, [.40, .008, .51], [0, y - .035, .122]);
      for (const x of [-.105, .105]) {
        box(hardware, chip, [.157, .023, .14], [x, y - .018, .051]);
        box(hardware, chipPlate, [.133, .008, .112], [x, y - .002, .051]);
      }
      for (const x of [-.105, -.035, .035, .105]) {
        box(hardware, socket, [.033, .015, .106], [x, y - .017, .216]);
      }
      hardware.visible = false;
      trays.set(gpu.gpu_id, { drawer, face, badge, hardware, marker, state: 'clear' });
    }
  }

  const labels = document.getElementById('rack-labels');
  const rackLabels = racks.map((rack) => {
    const item = document.createElement('span');
    item.className = 'rack-label';
    item.textContent = rack.label;
    labels.append(item);
    return item;
  });
  const raycaster = new Raycaster();
  const mouse = new Vector2();
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  let selected = null;
  let hovered = null;
  let focusedRack = null;
  let cameraMove = null;
  let pointerStart = null;
  let lastTime = 0;
  let frameSink = null;

  function cameraPreset(rackId = null, immediate = false) {
    const x = rackRoots.get(rackId)?.position.x ?? 0;
    const compact = camera.aspect < 1.32;
    const distance = rackId ? 4.15 : Math.max(5.05, 6.9 / Math.max(camera.aspect, .8));
    const target = new Vector3(x, rackId ? 1.03 : 1.05, .02);
    const eye = new Vector3(x + (rackId ? 1.62 : compact ? 1.6 : 2.2), rackId ? 2.3 : 2.9, distance);
    cameraMove = { target, eye, startTarget: controls.target.clone(), startEye: camera.position.clone(), time: performance.now() };
    if (immediate || reduced.matches) {
      controls.target.copy(target);
      camera.position.copy(eye);
      cameraMove = null;
      controls.update();
    }
  }

  function resize() {
    const width = container.clientWidth;
    const height = container.clientHeight;
    if (!width || !height) return;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
    cameraPreset(focusedRack, true);
  }
  const sizes = new ResizeObserver(resize);
  sizes.observe(container);
  resize();

  function pick(event) {
    const rect = renderer.domElement.getBoundingClientRect();
    mouse.x = (event.clientX - rect.left) / rect.width * 2 - 1;
    mouse.y = -(event.clientY - rect.top) / rect.height * 2 + 1;
    raycaster.setFromCamera(mouse, camera);
    return raycaster.intersectObjects(hitFaces)[0]?.object.userData.gpuId ?? null;
  }

  controls.addEventListener('start', () => { cameraMove = null; });
  renderer.domElement.addEventListener('pointerdown', (event) => {
    pointerStart = [event.clientX, event.clientY];
  });
  renderer.domElement.addEventListener('pointermove', (event) => {
    const id = event.pointerType === 'touch' || event.buttons ? null : pick(event);
    hovered = id;
    renderer.domElement.classList.toggle('is-over-gpu', Boolean(id));
    const rect = container.getBoundingClientRect();
    onHover(id, event.clientX - rect.left, event.clientY - rect.top);
  });
  renderer.domElement.addEventListener('pointerleave', () => {
    hovered = null;
    renderer.domElement.classList.remove('is-over-gpu');
    onHover(null);
  });
  renderer.domElement.addEventListener('pointercancel', () => { pointerStart = null; });
  renderer.domElement.addEventListener('pointerup', (event) => {
    if (!pointerStart) return;
    const moved = Math.hypot(event.clientX - pointerStart[0], event.clientY - pointerStart[1]);
    pointerStart = null;
    if (moved > 6) return;
    const id = pick(event);
    if (id) onSelect(id);
  });
  renderer.domElement.addEventListener('webglcontextlost', () => {
    const message = document.getElementById('stage-status');
    message.textContent = 'The 3D view disconnected. Reload the page to restore it.';
    message.hidden = false;
  });

  renderer.setAnimationLoop((time) => {
    const delta = lastTime ? Math.min(.05, (time - lastTime) / 1000) : .016;
    lastTime = time;
    if (cameraMove) {
      const step = Math.min(1, (time - cameraMove.time) / 510);
      const smooth = 1 - (1 - step) ** 3;
      controls.target.lerpVectors(cameraMove.startTarget, cameraMove.target, smooth);
      camera.position.lerpVectors(cameraMove.startEye, cameraMove.eye, smooth);
      if (step === 1) cameraMove = null;
    }
    controls.update();
    for (const [id, tray] of trays) {
      const extend = id === selected ? .55 : 0;
      const slide = tray.drawer.position.z;
      tray.drawer.position.z = reduced.matches || Math.abs(extend - slide) < .001
        ? extend
        : slide + (extend - slide) * (1 - Math.exp(-delta * 11));
      tray.hardware.visible = tray.drawer.position.z > .085;
      const emphasis = id === selected ? .28 : id === hovered ? .25 : .029;
      tray.face.material.opacity = Math.max(emphasis, tray.state === 'fault' ? .44 : tray.state === 'warning' ? .17 : 0);
    }
    renderer.domElement.dataset.trayExtension = selected ? trays.get(selected).drawer.position.z.toFixed(2) : '0.00';
    const width = container.clientWidth;
    const height = container.clientHeight;
    for (const [i, rack] of racks.entries()) {
      const p = rackRoots.get(rack.id).position;
      cameraUp.set(p.x, 2.11, .32);
      projectPoint.copy(cameraUp).project(camera);
      const label = rackLabels[i];
      label.hidden = Math.abs(projectPoint.x) > .96 || projectPoint.y > .92;
      label.style.left = ((projectPoint.x + 1) * width / 2) + 'px';
      label.style.top = ((1 - projectPoint.y) * height / 2) + 'px';
    }
    renderer.render(stage, camera);
    frameSink?.(renderer.domElement, time);
  });

  return {
    setFrameSink(sink) { frameSink = sink; },
    select(id, rackId) {
      selected = id;
      renderer.domElement.dataset.selectedGpu = id ?? '';
      rackLabels.forEach((label, i) => label.classList.toggle('is-current', racks[i].id === rackId));
      if (id && window.innerWidth <= 760 && focusedRack !== rackId) {
        focusedRack = rackId;
        cameraPreset(rackId);
      }
    },
    focus(rackId) {
      focusedRack = rackId;
      cameraPreset(rackId);
    },
    update(diagnostics, rackState) {
      const faults = [];
      for (const [id, tray] of trays) {
        tray.state = visualState(diagnostics.get(id)?.kind);
        tray.marker.visible = tray.state === 'fault';
        if (tray.marker.visible) faults.push(id);
        tray.face.material.color.setHex(palette[tray.state]);
        tray.badge.color.setHex(palette[tray.state]);
        tray.badge.emissive.setHex(palette[tray.state]).multiplyScalar(.22);
      }
      renderer.domElement.dataset.faultGpuIds = faults.join(' ');
      for (const [i, rack] of racks.entries()) {
        const state = rackState(rack);
        const mat = rackLights.get(rack.id);
        mat.color.setHex(palette[state]);
        mat.emissive.setHex(palette[state]).multiplyScalar(.25);
        rackLabels[i].classList.toggle('warning', state === 'warning');
        rackLabels[i].classList.toggle('fault', state === 'fault');
      }
    },
  };
}
