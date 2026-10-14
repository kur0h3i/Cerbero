// Tema elegido a mano (si lo hay); si no, el del sistema (ver app.css).
(function () {
  try {
    var t = localStorage.getItem('cerbero_theme');
    if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  } catch (e) {
    // Sin almacenamiento (modo privado...): se sigue el tema del sistema.
  }
})();
