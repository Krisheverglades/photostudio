(() => {
  const root = document.documentElement;
  const trigger = document.getElementById('themeTrigger');
  const menu = document.getElementById('themeMenu');
  const stage = document.getElementById('cameraStage');
  const camera = document.getElementById('cameraModel');
  const themes = ['noir', 'editorial', 'gallery'];
  const savedTheme = localStorage.getItem('portfolio-theme');
  if (themes.includes(savedTheme)) root.dataset.theme = savedTheme;
  trigger?.addEventListener('click', () => {
    const open = trigger.getAttribute('aria-expanded') === 'true';
    trigger.setAttribute('aria-expanded', String(!open));
    menu.classList.toggle('is-open', !open);
  });
  document.querySelectorAll('[data-set-theme]').forEach(button => button.addEventListener('click', () => {
    root.dataset.theme = button.dataset.setTheme;
    localStorage.setItem('portfolio-theme', button.dataset.setTheme);
    menu.classList.remove('is-open'); trigger.setAttribute('aria-expanded', 'false');
  }));
  document.addEventListener('click', event => { if (!event.target.closest('.header-actions')) { menu?.classList.remove('is-open'); trigger?.setAttribute('aria-expanded', 'false'); } });
  if (stage && camera) {
    stage.addEventListener('pointermove', event => {
      const rect = stage.getBoundingClientRect(); const x = (event.clientX - rect.left) / rect.width - .5; const y = (event.clientY - rect.top) / rect.height - .5;
      camera.style.setProperty('--rotate-y', `${x * 30}deg`); camera.style.setProperty('--rotate-x', `${-y * 18}deg`);
    });
    stage.addEventListener('pointerleave', () => { camera.style.setProperty('--rotate-y', '-8deg'); camera.style.setProperty('--rotate-x', '4deg'); });
  }
  document.getElementById('year').textContent = new Date().getFullYear();
})();
