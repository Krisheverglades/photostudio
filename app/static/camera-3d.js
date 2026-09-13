import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.180.0/build/three.module.js';

const canvas = document.getElementById('cameraCanvas');
const stage = document.getElementById('cameraStage');
const loading = document.getElementById('cameraLoading');
const reset = document.getElementById('cameraReset');

if (canvas && stage) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.15;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, .1, 100);
  camera.position.set(0, .25, 8.6);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x1a1a1a, 2.6));
  const key = new THREE.DirectionalLight(0xffffff, 5.5); key.position.set(-4, 6, 7); scene.add(key);
  const rim = new THREE.DirectionalLight(0x94b6ff, 4); rim.position.set(5, 1, -4); scene.add(rim);

  const bodyMat = new THREE.MeshStandardMaterial({ color:0x17191b, roughness:.48, metalness:.58 });
  const darkMat = new THREE.MeshStandardMaterial({ color:0x070809, roughness:.32, metalness:.74 });
  const ringMat = new THREE.MeshStandardMaterial({ color:0x33373b, roughness:.28, metalness:.92 });
  const glassMat = new THREE.MeshPhysicalMaterial({ color:0x102b30, roughness:.08, metalness:.15, transmission:.22, clearcoat:1, clearcoatRoughness:.08 });
  const accentMat = new THREE.MeshStandardMaterial({ color:0xdfe2e4, roughness:.3, metalness:.7 });
  const model = new THREE.Group(); scene.add(model);

  const mesh = (geometry, material, position, rotation=[0,0,0]) => {
    const item = new THREE.Mesh(geometry, material); item.position.set(...position); item.rotation.set(...rotation); item.castShadow=true; item.receiveShadow=true; model.add(item); return item;
  };
  mesh(new THREE.BoxGeometry(4.6,2.65,1.25,8,8,4), bodyMat, [0,0,0]);
  mesh(new THREE.BoxGeometry(1.05,2.45,1.65,5,8,5), darkMat, [2.05,-.05,.12]);
  mesh(new THREE.BoxGeometry(1.55,.65,1.05,5,4,4), bodyMat, [-.2,1.55,-.02]);
  mesh(new THREE.CylinderGeometry(.34,.42,.8,32), darkMat, [-.2,1.92,.02], [Math.PI/2,0,0]);
  mesh(new THREE.CylinderGeometry(.38,.38,.16,32), accentMat, [1.35,1.38,.22], [Math.PI/2,0,0]);
  mesh(new THREE.CylinderGeometry(1.35,1.35,.3,64), ringMat, [-.25,0,.83], [Math.PI/2,0,0]);
  mesh(new THREE.CylinderGeometry(1.18,1.30,1.65,64), darkMat, [-.25,0,1.72], [Math.PI/2,0,0]);
  mesh(new THREE.CylinderGeometry(1.10,1.15,.23,64), ringMat, [-.25,0,2.58], [Math.PI/2,0,0]);
  mesh(new THREE.CylinderGeometry(.96,1.03,.12,64), glassMat, [-.25,0,2.74], [Math.PI/2,0,0]);
  for (let i=0;i<8;i++) mesh(new THREE.BoxGeometry(.05,.03,.03), accentMat, [-.25+Math.cos(i*Math.PI/4)*1.19,Math.sin(i*Math.PI/4)*1.19,2.7]);
  mesh(new THREE.BoxGeometry(.58,.12,.35,3,2,2), darkMat, [-1.7,1.36,.2], [0,0,-.08]);

  const floor = new THREE.Mesh(new THREE.CircleGeometry(4.2,64), new THREE.ShadowMaterial({ color:0x000000, opacity:.28 }));
  floor.rotation.x=-Math.PI/2; floor.position.y=-1.65; floor.receiveShadow=true; scene.add(floor);
  renderer.shadowMap.enabled=true; renderer.shadowMap.type=THREE.PCFSoftShadowMap;

  let targetX=.08, targetY=-.42, zoom=8.6, dragging=false, lastX=0, lastY=0;
  model.rotation.set(targetX,targetY,0);
  const resize=()=>{ const rect=stage.getBoundingClientRect(); renderer.setSize(rect.width,rect.height,false); camera.aspect=rect.width/rect.height; camera.updateProjectionMatrix(); };
  new ResizeObserver(resize).observe(stage); resize();
  stage.addEventListener('pointerdown',e=>{dragging=true;lastX=e.clientX;lastY=e.clientY;stage.setPointerCapture(e.pointerId)});
  stage.addEventListener('pointermove',e=>{if(!dragging)return;targetY+=(e.clientX-lastX)*.009;targetX=Math.max(-.55,Math.min(.55,targetX+(e.clientY-lastY)*.006));lastX=e.clientX;lastY=e.clientY});
  stage.addEventListener('pointerup',()=>dragging=false); stage.addEventListener('pointercancel',()=>dragging=false);
  stage.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(6.3,Math.min(11,zoom+e.deltaY*.006))},{passive:false});
  reset?.addEventListener('click',()=>{targetX=.08;targetY=-.42;zoom=8.6});
  const animate=()=>{model.rotation.x+=(targetX-model.rotation.x)*.08;model.rotation.y+=(targetY-model.rotation.y)*.08;camera.position.z+=(zoom-camera.position.z)*.08;renderer.render(scene,camera);requestAnimationFrame(animate)};
  loading?.remove(); animate();
}
