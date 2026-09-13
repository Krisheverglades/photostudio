(() => {
  if (document.getElementById('glass-photo-scene')) return;
  const scene = document.createElement('div');
  scene.id = 'glass-photo-scene'; scene.setAttribute('aria-hidden', 'true');
  scene.innerHTML = '<div class="glass-photo-window"><div class="glass-photo-track"></div></div><div class="glass-photo-veil"></div>';
  document.body.prepend(scene); document.body.classList.add('glass-background-page');
  const track = scene.querySelector('.glass-photo-track');
  const titles = [...document.querySelectorAll('.admin-header, body.studio-page > .nav')];
  titles.forEach(title => title.classList.add('glass-adaptive-title'));
  const luminance = new Map();
  function measure(img) {
    try {
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 8;
      const ctx = canvas.getContext('2d', {willReadFrequently:true}); ctx.drawImage(img, 0, 0, 8, 8);
      const data = ctx.getImageData(0, 0, 8, 8).data; let sum = 0;
      for (let i=0; i<data.length; i+=4) sum += .2126 * data[i] + .7152 * data[i+1] + .0722 * data[i+2];
      luminance.set(img.src, sum / 64);
    } catch { luminance.set(img.src, 90); }
  }
  fetch('/api/public-backgrounds', {credentials:'same-origin'}).then(r => {
    if (!r.ok) throw new Error('Background unavailable'); return r.json();
  }).then(({images}) => {
    if (!images.length) return;
    let photos = images;
    while (photos.length < 3) photos = photos.concat(images);
    const duration = photos.length * 11;
    track.style.setProperty('--glass-duration', `${duration}s`);
    [...photos, ...photos].forEach(src => {
      const frame = document.createElement('div'); frame.className = 'glass-photo-frame';
      const img = document.createElement('img'); img.src = src; img.alt = ''; img.decoding = 'async';
      img.addEventListener('load', () => measure(img), {once:true});
      frame.append(img); track.append(frame);
    });
    const started = performance.now();
    const adapt = () => {
      const index = Math.floor((performance.now() - started) / 11000) % photos.length;
      const average = [0,1,2].reduce((sum, offset) => sum + (luminance.get(new URL(photos[(index+offset)%photos.length], location.href).href) ?? 90), 0) / 3;
      titles.forEach(title => title.classList.toggle('over-light-photo', average > 145));
    };
    adapt(); setInterval(adapt, 1200);
  }).catch(() => scene.classList.add('without-photos'));

  titles.forEach(title => {
    title.addEventListener('pointermove', event => {
      if (event.pointerType === 'touch') return;
      const box = title.getBoundingClientRect();
      title.style.setProperty('--title-y', `${((event.clientX-box.left)/box.width-.5)*7}deg`);
      title.style.setProperty('--title-x', `${-((event.clientY-box.top)/box.height-.5)*5}deg`);
    });
    title.addEventListener('pointerleave', () => { title.style.setProperty('--title-y','0deg'); title.style.setProperty('--title-x','0deg'); });
  });
  let lastFlash = 0;
  document.addEventListener('pointerdown', event => {
    if (event.button !== 0 || performance.now()-lastFlash < 350) return;
    if (!event.target.closest('a,button,input,select,textarea,label,[role="button"]')) return;
    lastFlash = performance.now();
    const flash = document.createElement('div'); flash.className = 'camera-click-flash';
    flash.style.left = `${event.clientX}px`; flash.style.top = `${event.clientY}px`;
    document.body.append(flash); setTimeout(() => flash.remove(), 250);
  }, {passive:true});
})();
