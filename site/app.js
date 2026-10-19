// Design gallery: one shared three.js viewer over twelve designs that each keep their own
// identity. A design's id is its filename stub, its URL hash and its key in localStorage,
// so a selection is shareable and survives a reload.

import { DESIGNS } from "./designs.js";
import { latestMeshLoader } from "./latest-load.js";

const STORE_KEY = "soccer-modeling:chosen";

const cardsEl = document.getElementById("cards");
const stageEl = document.getElementById("stage");
const noteEl = document.getElementById("stageLoading");
const metaEl = document.getElementById("stageMeta");
const choiceEmpty = document.getElementById("choiceEmpty");
const choiceBox = document.getElementById("choiceBox");
const btnRotate = document.getElementById("btnRotate");
const btnWire = document.getElementById("btnWire");
const btnReset = document.getElementById("btnReset");
const btnCopy = document.getElementById("btnCopy");
const btnClear = document.getElementById("btnClear");
const exagEl = document.getElementById("exag");
const exagVal = document.getElementById("exagVal");

let chosen = load(STORE_KEY);
let current = null;      // design shown in the viewer
let viewer = null;       // three.js handle, or null when it could not start
let fallback = false;    // true once we know WebGL/CDN is unavailable
let exaggeration = Number(exagEl.value) || 1;   // view-only vertical stretch

/* ------------------------------ storage ------------------------------ */

function load(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function save(key, value) {
  try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); }
  catch { /* private mode: selection just will not persist */ }
}

function buildCommand(design) {
  const flags = design.flags ? ` ${design.flags}` : "";
  return `uv run python -m terrace.pitch_print build --player "Wirtz" --map touch \\\n`
       + `  --style terrain${flags} --name design_${design.id}`;
}

const designById = (id) => DESIGNS.find((design) => design.id === id) || null;
const sizeText = (design) =>
  `${design.size[0].toFixed(0)} × ${design.size[1].toFixed(0)} × ${design.size[2].toFixed(1)} mm`;

/* ------------------------------ viewer ------------------------------ */

async function startViewer() {
  const THREE = await import("three");
  const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");
  const { STLLoader } = await import("three/addons/loaders/STLLoader.js");

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  stageEl.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 1, 6000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.9;
  controls.maxPolarAngle = Math.PI * 0.495;   // never look under the base plate

  scene.add(new THREE.AmbientLight(0xffffff, 1.5));
  const key = new THREE.DirectionalLight(0xffffff, 2.3);
  key.position.set(160, 260, 220);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xffffff, 1.0);
  rim.position.set(-220, 140, -180);
  scene.add(rim);

  const material = new THREE.MeshStandardMaterial({
    color: 0xd6d6d6, roughness: 0.72, metalness: 0.02,
  });
  const loader = new STLLoader();

  let mesh = null;
  let grid = null;

  function resize() {
    const w = stageEl.clientWidth || 1;
    const h = stageEl.clientHeight || 1;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(stageEl);
  resize();

  function place(geometry) {
    geometry.rotateX(-Math.PI / 2);              // the STL is Z-up, the viewport is Y-up
    geometry.computeBoundingBox();
    const box = geometry.boundingBox;
    const centre = box.getCenter(new THREE.Vector3());
    geometry.translate(-centre.x, -box.min.y, -centre.z);   // centred, base resting on y = 0
    geometry.computeBoundingSphere();
    return geometry.boundingSphere.radius;
  }

  let lastRadius = 0;

  function fitCamera(design, radius) {
    lastRadius = radius || lastRadius;
    const distance = lastRadius * (3.1 + 0.3 * (exaggeration - 1));
    camera.position.set(distance * 0.6, distance * 0.52, distance * 0.6);
    controls.target.set(0, design.size[2] * exaggeration * 0.3, 0);
    camera.updateProjectionMatrix();
    controls.update();
    controls.saveState();      // so "reset view" returns to this fitted view
  }

  const show = latestMeshLoader(loader, (geometry, design) => {
    const radius = place(geometry);
    if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); }
    if (grid) { scene.remove(grid); grid.geometry.dispose(); grid.material.dispose(); }
    mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);
    const span = Math.max(design.size[0], design.size[1]) * 1.35;
    grid = new THREE.GridHelper(span, 10, 0x6f7683, 0x3b4149);
    grid.material.opacity = 0.3;
    grid.material.transparent = true;
    scene.add(grid);
    mesh.scale.y = exaggeration;
    fitCamera(design, radius);
  });

  (function loop() {
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(loop);
  })();

  return {
    show,
    setRotate: (on) => { controls.autoRotate = on; },
    setWire: (on) => { material.wireframe = on; },
    setExaggeration: (value) => {
      // Stretch and re-fit only: the mesh is already in memory, so nothing refetches.
      if (mesh) { mesh.scale.y = value; fitCamera(current, lastRadius); }
      metaEl.textContent = `${current.title} · ${current.id} · `
        + `${current.tris.toLocaleString()} triangles`
        + (value === 1 ? " · true scale" : ` · height ×${value} for viewing`);
    },
    reset: () => controls.reset(),
  };
}

/* ------------------------------ cards ------------------------------ */

const chip = (text, warn = false) => `<span class="chip${warn ? " warn" : ""}">${text}</span>`;

function renderCards() {
  cardsEl.innerHTML = "";
  for (const design of DESIGNS) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.id = design.id;
    card.innerHTML = `
      <img src="${design.thumb}" alt="${design.title}: ${design.note}" loading="lazy">
      <h3>${design.title} <span class="tick" hidden>chosen</span></h3>
      <code class="id">${design.id}</code>
      <p class="note">${design.note}</p>
      <div class="chips">
        ${chip(design.family)}
        ${chip(sizeText(design))}
        ${chip(`${design.grams} g solid`)}
        ${design.lossy ? chip("lossy: banded", true) : ""}
      </div>
      <div class="card-actions">
        <button data-preview="${design.id}">Preview</button>
        <button class="primary" data-choose="${design.id}">Choose this one</button>
      </div>`;

    card.querySelector("[data-preview]").addEventListener("click", () => select(design.id, { hash: true }));
    card.querySelector("[data-choose]").addEventListener("click", () => choose(design.id));
    cardsEl.appendChild(card);
  }
  paint();
}

/* ------------------------------ state ------------------------------ */

function select(id, { hash = false } = {}) {
  const design = designById(id);
  if (!design) return;
  current = design;
  if (hash) history.replaceState(null, "", `#${design.id}`);

  for (const card of cardsEl.children) {
    card.classList.toggle("active", card.dataset.id === design.id);
  }
  metaEl.textContent = `${design.title} · ${design.id} · ${design.tris.toLocaleString()} triangles`
    + (exaggeration === 1 ? " · true scale" : ` · height ×${exaggeration} for viewing`);

  if (viewer) {
    noteEl.textContent = "loading mesh…";
    noteEl.classList.remove("hidden");
    viewer.show(design).then(loaded => {
      if (current?.id !== design.id) return;
      if (loaded) noteEl.classList.add("hidden");
      else showFallback(design);
    });
  } else if (fallback) {
    showFallback(design);
  }
  paint();
}

function showFallback(design) {
  noteEl.classList.remove("hidden");
  const image = document.createElement("img");
  image.src = design.thumb; image.alt = design.title;
  const message = document.createElement("p");
  message.textContent = "3D preview unavailable. Showing the image instead.";
  const content = document.createElement("div");
  content.append(image, message);
  noteEl.replaceChildren(content);
}

function choose(id) {
  chosen = chosen === id ? null : id;   // clicking the chosen one again clears it
  save(STORE_KEY, chosen);
  if (current?.id !== id) select(id, { hash: true });
  else { history.replaceState(null, "", `#${id}`); paint(); }
}

function paint() {
  for (const card of cardsEl.children) {
    const isChosen = card.dataset.id === chosen;
    card.classList.toggle("chosen", isChosen);
    card.querySelector(".tick").hidden = !isChosen;
    const button = card.querySelector("[data-choose]");
    button.classList.toggle("done", isChosen);
    button.textContent = isChosen ? "Chosen ✓" : "Choose this one";
  }

  const design = designById(chosen);
  choiceEmpty.hidden = Boolean(design);
  choiceBox.hidden = !design;
  if (!design) return;
  document.getElementById("choiceTitle").textContent = design.title;
  document.getElementById("choiceId").textContent = design.id;
  document.getElementById("factSize").textContent = sizeText(design);
  document.getElementById("factMass").textContent = `${design.grams} g`;
  document.getElementById("factTris").textContent = `${design.tris.toLocaleString()} triangles`;
  document.getElementById("factFile").textContent = design.file.replace("../", "");
  document.getElementById("choiceCmd").textContent = buildCommand(design);
}

/* ------------------------------ wiring ------------------------------ */

function toggle(button, apply) {
  const on = !button.classList.contains("on");
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", String(on));
  apply(on);
}

btnRotate.addEventListener("click", () => toggle(btnRotate, (on) => viewer?.setRotate(on)));
btnWire.addEventListener("click", () => toggle(btnWire, (on) => viewer?.setWire(on)));
btnReset.addEventListener("click", () => {
  if (viewer) viewer.reset();
  else if (current) select(current.id);
});
btnClear.addEventListener("click", () => { chosen = null; save(STORE_KEY, null); paint(); });
exagEl.addEventListener("input", () => {
  exaggeration = Number(exagEl.value) || 1;
  exagVal.textContent = `×${exaggeration}`;
  if (viewer) viewer.setExaggeration(exaggeration);
  else if (current) select(current.id);
});
btnCopy.addEventListener("click", async () => {
  const design = designById(chosen);
  if (!design) return;
  try {
    await navigator.clipboard.writeText(buildCommand(design));
    btnCopy.textContent = "Copied";
  } catch {
    btnCopy.textContent = "Copy failed — select the text";
  }
  setTimeout(() => { btnCopy.textContent = "Copy command"; }, 1500);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  // The target is normally an element, but a programmatic dispatch can target document.
  const target = event.target instanceof Element ? event.target.closest("button") : null;
  if (target || event.metaKey || event.ctrlKey || event.altKey) return;
  if (!current) return;
  const index = DESIGNS.findIndex((design) => design.id === current.id);
  const step = event.key === "ArrowRight" ? 1 : DESIGNS.length - 1;
  select(DESIGNS[(index + step) % DESIGNS.length].id, { hash: true });
});

/* ------------------------------ boot ------------------------------ */

document.getElementById("headline").textContent =
  `${DESIGNS.length} ways to print the same map`;
renderCards();
select((designById(location.hash.slice(1)) || designById(chosen) || DESIGNS[0]).id, { hash: true });

startViewer()
  .then((handle) => {
    viewer = handle;
    if (current) select(current.id);
  })
  .catch(() => {
    fallback = true;
    if (current) select(current.id);
  });
