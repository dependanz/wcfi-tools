import * as THREE from './vendor/three.module.min.js';

// Decorative scene only: every interactive control remains an accessible DOM button.
export function initScene(canvas){
  let renderer;
  try{renderer=new THREE.WebGLRenderer({canvas,alpha:true,antialias:true,powerPreference:'low-power'});}catch{document.body.dataset.scene='fallback';canvas.hidden=true;return {setPaused(){},setActive(){},select(){}};}
  renderer.setPixelRatio(Math.min(devicePixelRatio,1.5));renderer.setClearColor(0x000000,0);renderer.toneMapping=THREE.ACESFilmicToneMapping;renderer.toneMappingExposure=1.25;
  const scene=new THREE.Scene();scene.fog=new THREE.FogExp2(0x102426,.027);
  const camera=new THREE.PerspectiveCamera(34,1,.1,60),targetCamera=new THREE.Vector3(),look=new THREE.Vector3(0,-.05,0);
  const rig=new THREE.Group();scene.add(rig);
  const material=(color,roughness=.6,metalness=0)=>new THREE.MeshStandardMaterial({color,roughness,metalness});
  const moss=material(0x314c42),rim=material(0x718469,.45,.4),wood=material(0x4a4f36,.4,.15),gold=material(0xd6b96d,.24,.65),paper=material(0xf1e5b9,.85),ink=material(0x5b674c);
  const glow=new THREE.MeshBasicMaterial({color:0xe0c586,transparent:true,opacity:.5});
  function box(w,h,d,mat,x,y,z,parent=rig){const mesh=new THREE.Mesh(new THREE.BoxGeometry(w,h,d),mat);mesh.position.set(x,y,z);parent.add(mesh);return mesh;}
  function cylinder(rt,rb,h,mat,x,y,z,parent=rig){const mesh=new THREE.Mesh(new THREE.CylinderGeometry(rt,rb,h,64),mat);mesh.position.set(x,y,z);parent.add(mesh);return mesh;}
  cylinder(2.55,2.65,.2,moss,0,-1.9,0);cylinder(2.49,2.49,.035,rim,0,-1.78,0);cylinder(2.45,2.45,.04,moss,0,-1.75,0);
  const floor=new THREE.Mesh(new THREE.PlaneGeometry(45,45),material(0x112b2b));floor.rotation.x=-Math.PI/2;floor.position.y=-2.04;scene.add(floor);
  for(let j=0;j<3;j++){const ring=new THREE.Mesh(new THREE.TorusGeometry(2.08+j*.18,.013,6,100),glow);ring.rotation.x=Math.PI/2;ring.position.y=-1.71;rig.add(ring);}
  cylinder(.065,.085,1.9,gold,0,-.7,0);
  for(let i=0;i<3;i++){const angle=i*Math.PI*2/3;const leg=box(.055,.05,.78,gold,Math.sin(angle)*.32,-1.69,Math.cos(angle)*.32);leg.rotation.y=angle;}
  const desk=new THREE.Group();desk.position.y=.32;desk.rotation.x=.52;rig.add(desk);box(1.7,.08,1.17,wood,0,0,0,desk);box(1.76,.06,.09,gold,0,.06,.57,desk);
  const book=new THREE.Group();book.position.y=.08;desk.add(book);
  for(const side of [-1,1]){const leaf=new THREE.Group();leaf.position.x=side*.39;leaf.rotation.z=-side*.08;book.add(leaf);box(.77,.035,1.03,paper,0,0,0,leaf);for(let j=0;j<7;j++)box(.53,.003,.006,ink,0,.02,-.34+j*.1,leaf);for(let j=0;j<5;j++){const note=new THREE.Mesh(new THREE.SphereGeometry(.022,10,8),ink);note.scale.set(1.4,.12,1);note.position.set(-.2+j*.09,.024,-.25+(j%3)*.1);leaf.add(note);}}
  for(let i=0;i<3;i++){const volume=box(.65,.09,.85,i===1?paper:wood,-1.25,-1.62+i*.1,.45);volume.rotation.y=.1+i*.12;}
  cylinder(.28,.32,.07,gold,1.45,-1.66,-.55);cylinder(.024,.024,2.35,gold,1.45,-.47,-.55);
  const shade=new THREE.Mesh(new THREE.ConeGeometry(.34,.32,32,1,true),gold);shade.position.set(1.45,.86,-.55);shade.rotation.z=.12;rig.add(shade);
  const bulb=new THREE.Mesh(new THREE.SphereGeometry(.075,16,12),new THREE.MeshBasicMaterial({color:0xffe1a0}));bulb.position.set(1.43,.71,-.55);rig.add(bulb);
  const lamp=new THREE.PointLight(0xffd493,11,7,2);lamp.position.copy(bulb.position);rig.add(lamp);
  for(let j=0;j<3;j++){const arch=new THREE.Mesh(new THREE.TorusGeometry(2.15+j*.19,.012,6,80,Math.PI),glow);arch.position.set(0,-.53,-1.2-j*.12);rig.add(arch);}
  const notes=[];
  for(let i=0;i<5;i++){const note=new THREE.Group(),head=new THREE.Mesh(new THREE.SphereGeometry(.09,16,12),gold);head.scale.set(1.45,.8,.7);head.rotation.z=.3;note.add(head);cylinder(.009,.009,.43,gold,.1,.21,0,note);note.position.set(-.95+i*.4,1.05+Math.sin(i*.7)*.22,-.2+Math.cos(i)*.22);note.userData.y=note.position.y;notes.push(note);rig.add(note);}
  const particles=new THREE.BufferGeometry(),positions=[];for(let i=0;i<90;i++)positions.push(Math.sin(i*3.11)*9,Math.cos(i*1.77)*6,Math.sin(i*2.3)*3-2);
  particles.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));const dust=new THREE.Points(particles,new THREE.PointsMaterial({color:0xd9e8c3,size:.019,transparent:true,opacity:.5}));scene.add(dust);
  scene.add(new THREE.HemisphereLight(0xb6c9a5,0x102b2d,2));const key=new THREE.DirectionalLight(0xf9e4b3,3);key.position.set(-3,6,5);scene.add(key);const fill=new THREE.DirectionalLight(0x6caca8,2);fill.position.set(4,2,-3);scene.add(fill);
  let paused=false,active=true,raf=0,last=0,clock=0,pointerX=0,choice=0,transitionMs=0,lost=false;
  function composition(){const narrow=innerWidth<=600,landscape=innerHeight<=450;rig.position.set(narrow?.95:2.4,narrow?2:.45,narrow?-2.4:0);rig.scale.setScalar(narrow?.42:.8);if(!narrow&&!landscape){const aspect=innerWidth/innerHeight;rig.position.x=aspect*1.45;rig.scale.setScalar(Math.min(.8,aspect*.5));}if(landscape){rig.position.set(.2,-.1,-4);rig.scale.setScalar(.85);}targetCamera.set(active?0:.45,active?1.1:1.3,active?10.3:8.8);}
  function draw(){if(lost)return;renderer.render(scene,camera);document.body.dataset.scene='webgl';}
  function frame(time){raf=0;if(paused||document.hidden||lost)return;const elapsed=time-last;if(elapsed>=32){last=time;const dt=Math.min(elapsed,60);clock+=dt*.0003;for(let i=0;i<notes.length;i++)notes[i].position.y=notes[i].userData.y+Math.sin(clock*2+i)*.06;dust.rotation.y=clock*.018;rig.rotation.y=THREE.MathUtils.lerp(rig.rotation.y,Math.sin(clock*.5)*.035+pointerX*.06+(choice-1.5)*.025,.1);camera.position.lerp(targetCamera,.12);camera.lookAt(look);transitionMs-=dt;draw();}if(active||transitionMs>0)raf=requestAnimationFrame(frame);}
  function update(){cancelAnimationFrame(raf);raf=0;composition();canvas.style.opacity=active?'1':'.28';document.body.dataset.motion=paused?'paused':active?'playing':'resting';if(lost)return;if(paused){camera.position.copy(targetCamera);camera.lookAt(look);draw();}else if(!document.hidden){transitionMs=650;last=performance.now();raf=requestAnimationFrame(frame);}}
  function resize(){renderer.setSize(innerWidth,innerHeight,false);camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();composition();camera.position.copy(targetCamera);camera.lookAt(look);draw();}
  addEventListener('resize',resize);addEventListener('pointermove',e=>{if(e.pointerType==='mouse')pointerX=e.clientX/innerWidth-.5;},{passive:true});document.addEventListener('visibilitychange',update);
  canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();lost=true;cancelAnimationFrame(raf);canvas.hidden=true;document.body.dataset.scene='fallback';});
  resize();update();
  return {setPaused(value){paused=value;update();},setActive(value){active=value;update();},select(route){const palette={lineups:0xd6b96d,maker:0xedc392,catalogue:0x94cbc0,resources:0xd7d9ad};choice=Math.max(0,Object.keys(palette).indexOf(route));gold.color.setHex(palette[route]||palette.lineups);lamp.color.setHex(route==='catalogue'?0xbce6d3:0xffd493);update();}};
}
