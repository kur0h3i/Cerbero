// Cerbero: la página de detalle. Solo lee la API (/api/health, /api/alerts,
// /api/status) cada 10 s y la pinta. Sin dependencias ni compilación.
//
// Todo el DOM se construye con textContent: los mensajes llevan texto que viene
// de fuera (nombres de usuario de SSH, salidas de healthchecks...).
'use strict';

const REFRESH_MS = 10_000;
const TIMEOUT_MS = 6_000;

const HEADS = [
  { id: 'recursos', name: 'Recursos', section: 'sec-recursos' },
  { id: 'contenedores', name: 'Contenedores', section: 'sec-contenedores' },
  { id: 'accesos', name: 'Accesos SSH', section: 'sec-accesos' },
  { id: 'smart', name: 'Discos (SMART)', section: 'sec-smart' },
  { id: 'tareas', name: 'Tareas', section: 'sec-tareas' },
];
const HEAD_NAME = Object.fromEntries(HEADS.map((h) => [h.id, h.name]));

// Forma + texto + color: un estado nunca se distingue solo por el color.
const LEVEL = {
  critical: { cls: 'critical', icon: '✕', text: 'Crítico' },
  warning: { cls: 'warning', icon: '▲', text: 'Aviso' },
  info: { cls: 'info', icon: 'ℹ', text: 'Info' },
  ok: { cls: 'ok', icon: '✓', text: 'OK' },
  unknown: { cls: 'unknown', icon: '?', text: 'Sin datos' },
};
const SEVERITY = { info: 0, warning: 1, critical: 2 };

// --- Utilidades ---------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else el.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function badge(level, text) {
  const l = LEVEL[level] || LEVEL.unknown;
  return h('span', { class: `badge ${l.cls}` }, `${l.icon} ${text || l.text}`);
}

function replace(id, ...children) {
  const box = document.getElementById(id);
  // El repintado borra el elemento con foco: se busca su equivalente para no perderlo.
  const had = box.contains(document.activeElement) ? document.activeElement : null;
  const key = (el) => el.getAttribute('href') ?? el.tagName;
  const wanted = had && key(had);
  box.replaceChildren(...children.flat().filter(Boolean));
  if (wanted) {
    [...box.querySelectorAll('a[href], summary')]
      .find((el) => key(el) === wanted)
      ?.focus({ preventScroll: true });
  }
}

const fmt = new Intl.NumberFormat('es-ES', { maximumFractionDigits: 1 });
const num = (n) => (n == null ? '—' : fmt.format(n));

function ago(iso) {
  if (!iso) return 'nunca';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `hace ${Math.round(s)} s`;
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 48 * 3600) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} días`;
}

function when(iso) {
  return iso
    ? new Date(iso).toLocaleString('es-ES', { dateStyle: 'short', timeStyle: 'medium' })
    : '';
}

function duration(fromIso, toIso) {
  const m = (new Date(toIso) - new Date(fromIso)) / 60000;
  if (m < 1) return 'menos de 1 min';
  if (m < 90) return `${Math.round(m)} min`;
  if (m < 48 * 60) return `${Math.round(m / 60)} h`;
  return `${Math.round(m / 1440)} días`;
}

function every(seconds) {
  if (!seconds) return '';
  if (seconds % 86400 === 0) return seconds === 604800 ? 'semanal' : `cada ${seconds / 86400} días`;
  if (seconds % 3600 === 0) return `cada ${seconds / 3600} h`;
  return `cada ${Math.round(seconds / 60)} min`;
}

function timeEl(iso, text) {
  return h('time', { datetime: iso, title: when(iso) }, text || ago(iso));
}

function empty(text) {
  return h('p', { class: 'empty' }, text);
}

/** Medidor con la marca del umbral. ``severity`` decide el color del relleno. */
function meter(label, value, max, mark, severity, valueText) {
  const pct = Math.max(0, Math.min(100, (value / max) * 100));
  const fill = h('div', { class: `meter-fill ${severity || ''}` });
  fill.style.width = `${pct}%`;
  const track = h(
    'div',
    {
      class: 'meter',
      role: 'meter',
      'aria-label': label,
      'aria-valuemin': 0,
      'aria-valuemax': max,
      'aria-valuenow': value,
      'aria-valuetext': valueText,
    },
    fill,
  );
  if (mark != null) {
    const m = h('div', { class: 'meter-mark', title: 'Umbral' });
    m.style.left = `${Math.max(0, Math.min(100, (mark / max) * 100))}%`;
    track.append(m);
  }
  return h(
    'div',
    { class: 'metric' },
    h(
      'div',
      { class: 'metric-head' },
      h('span', { class: 'metric-label' }, label),
      h('span', { class: 'metric-value' }, valueText),
    ),
    track,
  );
}

/** Gravedad de un valor frente a su umbral (alto = malo). */
function severityOf(value, threshold) {
  if (value == null) return '';
  if (value > threshold) return 'critical';
  if (value > threshold - 10) return 'warning';
  return '';
}

function stat(label, value) {
  return h(
    'div',
    { class: 'stat' },
    h('span', { class: 'stat-label' }, label),
    h('span', { class: 'stat-value' }, value),
  );
}

/** ``headers``: [texto, clase]; la clase se repite en cada celda de esa columna. */
function table(headers, rows) {
  for (const row of rows) {
    [...row.children].forEach((td, i) => {
      if (headers[i] && headers[i][1]) td.classList.add(...headers[i][1].split(' '));
    });
  }
  return h(
    'div',
    { class: 'table-wrap' },
    h(
      'table',
      {},
      h('thead', {}, h('tr', {}, headers.map(([text, cls]) => h('th', { class: cls }, text)))),
      h('tbody', {}, rows),
    ),
  );
}

// --- Datos --------------------------------------------------------------------------

async function getJson(path) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(path, { signal: ctrl.signal, cache: 'no-store' });
    if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

let last = null;
let lastOk = null;

async function refresh() {
  try {
    const [health, alerts, status] = await Promise.all([
      getJson('/api/health'),
      getJson('/api/alerts'),
      getJson('/api/status'),
    ]);
    last = { health, alerts: alerts.alerts || [], status };
    lastOk = new Date();
    document.getElementById('offline').hidden = true;
    render(last);
  } catch (err) {
    console.warn('Cerbero no responde:', err);
    document.getElementById('offline').hidden = false;
  }
  renderUpdated();
}

// --- Pintado -------------------------------------------------------------------------

function render({ health, alerts, status }) {
  const active = alerts.filter((a) => a.active);
  const resolved = alerts.filter((a) => !a.active);
  document.getElementById('server').textContent = status.servidor;
  document.title = active.length ? `(${active.length}) Cerbero` : 'Cerbero';
  renderGlobal(health, active);
  renderHeads(health, active, status);
  renderActive(active);
  renderRecursos(status);
  renderSmart(status);
  renderAccesos(status);
  renderTareas(status);
  renderContenedores(status);
  renderHistory(resolved);
}

function worst(alerts) {
  return alerts.reduce(
    (acc, a) => (SEVERITY[a.level] > SEVERITY[acc] ? a.level : acc),
    'info',
  );
}

function renderGlobal(health, active) {
  const el = document.getElementById('global');
  const level = active.length ? worst(active) : health.status === 'ok' ? 'ok' : 'warning';
  const l = LEVEL[level === 'info' ? 'ok' : level];
  const text = active.length
    ? `${active.length} alerta${active.length > 1 ? 's' : ''} activa${active.length > 1 ? 's' : ''}`
    : health.status === 'ok'
      ? 'En guardia'
      : 'Vigilancia incompleta';
  document.body.dataset.state = l.cls;
  // Solo si cambia: reasignar el mismo texto también lo anuncia el lector de pantalla.
  const cls = `pill badge ${l.cls}`;
  const txt = `${l.icon} ${text}`;
  if (el.className !== cls) el.className = cls;
  if (el.textContent !== txt) el.textContent = txt;
}

function renderUpdated() {
  const el = document.getElementById('updated');
  el.textContent = lastOk ? `actualizado ${ago(lastOk.toISOString())}` : '';
}

function headSummary(id, status) {
  switch (id) {
    case 'recursos':
      return status.cpu_pct == null
        ? 'Leyendo…'
        : `CPU ${num(status.cpu_pct)} % · RAM ${num(status.ram_free_gb)} GB libres`;
    case 'contenedores': {
      const c = status.containers;
      if (!c.length) return 'Sin contenedores';
      const up = c.filter((x) => x.status === 'running').length;
      return `${up} de ${c.length} en marcha`;
    }
    case 'accesos': {
      const a = status.accesos;
      if (!a) return 'Leyendo…';
      if (!a.legible) return 'No puede leer auth.log';
      const lastLogin = a.ultimos_accesos[0];
      return `${a.fallos_ventana} fallos recientes · ${lastLogin ? `último acceso ${ago(lastLogin.fecha)}` : 'sin accesos desde el arranque'}`;
    }
    case 'smart': {
      const temps = status.smart.map((d) => d.temp_c).filter((t) => t != null);
      if (!status.smart.length) return 'Sin datos';
      return `${status.smart.length} discos${temps.length ? ` · máx. ${num(Math.max(...temps))} °C` : ''}`;
    }
    case 'tareas': {
      if (!status.tareas.length) return 'Sin informes';
      const ok = status.tareas.filter((t) => t.resultado === 'ok' && !t.parada).length;
      return `${ok} de ${status.tareas.length} al día`;
    }
    default:
      return '';
  }
}

function renderHeads(health, active, status) {
  const cards = HEADS.map(({ id, name, section }) => {
    const mine = active.filter((a) => a.head === id);
    const healthy = health.heads[id];
    let level = 'ok';
    let text;
    if (mine.length) {
      level = worst(mine);
      text = `${mine.length} alerta${mine.length > 1 ? 's' : ''}`;
    } else if (healthy === false) {
      level = 'unknown';
      text = 'Ciega';
    }
    return h(
      'a',
      { class: `head ${LEVEL[level].cls}`, href: `#${section}` },
      h('span', { class: 'head-name' }, name),
      badge(level, text),
      h('span', { class: 'head-line' }, headSummary(id, status)),
    );
  });
  replace('heads', cards);
}

function alertItem(a, history) {
  const meta = history
    ? [timeEl(a.timestamp, when(a.timestamp)), ` → ${duration(a.timestamp, a.resolved_at)}`]
    : ['desde ', timeEl(a.timestamp)];
  return h(
    'li',
    {},
    badge(history && a.level !== 'info' ? 'ok' : a.level, history && a.level !== 'info' ? 'Resuelta' : null),
    h('span', { class: 'alert-msg' }, h('strong', {}, `${HEAD_NAME[a.head] || a.head}: `), a.message),
    h(
      'span',
      { class: 'alert-meta' },
      ...meta,
      history && a.level !== 'info' ? ` · era ${LEVEL[a.level].text.toLowerCase()}` : null,
    ),
  );
}

function renderActive(active) {
  document.getElementById('active-count').textContent = active.length ? `(${active.length})` : '';
  const sorted = [...active].sort(
    (a, b) => SEVERITY[b.level] - SEVERITY[a.level] || b.timestamp.localeCompare(a.timestamp),
  );
  replace(
    'active',
    sorted.length
      ? sorted.map((a) => alertItem(a, false))
      : [h('li', {}, badge('ok'), h('span', {}, 'Sin alertas activas.'))],
  );
}

function renderHistory(resolved) {
  replace(
    'history',
    resolved.length
      ? resolved.map((a) => alertItem(a, true))
      : [h('li', {}, h('span', { class: 'faint' }, '—'), h('span', { class: 'faint' }, 'Nada resuelto todavía.'))],
  );
}

function renderRecursos(status) {
  const u = status.umbrales;
  if (status.cpu_pct == null) {
    replace('recursos', empty('Esperando la primera lectura…'));
    return;
  }
  const used = status.ram_total_gb - status.ram_free_gb;
  const ramMark = status.ram_total_gb - u.ram_libre_min_gb;
  const ramSeverity =
    status.ram_free_gb < u.ram_libre_min_gb
      ? 'critical'
      : status.ram_free_gb < u.ram_libre_min_gb * 1.5
        ? 'warning'
        : '';
  const items = [
    meter(
      'CPU',
      status.cpu_pct,
      100,
      u.cpu_pct,
      severityOf(status.cpu_pct, u.cpu_pct),
      `${num(status.cpu_pct)} % · umbral ${num(u.cpu_pct)} % (${u.cpu_lecturas} lecturas)`,
    ),
    meter(
      'RAM',
      used,
      status.ram_total_gb,
      ramMark,
      ramSeverity,
      `${num(status.ram_free_gb)} GB disponibles de ${num(status.ram_total_gb)} · mínimo ${num(u.ram_libre_min_gb)} GB`,
    ),
  ];
  for (const d of status.disks) {
    if (d.error) {
      items.push(
        h(
          'div',
          { class: 'metric' },
          h('div', { class: 'metric-head' }, h('span', { class: 'metric-label' }, d.mount), badge('warning', d.error)),
        ),
      );
    } else {
      items.push(
        meter(
          d.mount,
          d.percent,
          100,
          u.disco_pct,
          severityOf(d.percent, u.disco_pct),
          `${num(d.percent)} % · ${num(d.free_gb)} GB libres de ${num(d.total_gb)}`,
        ),
      );
    }
  }
  replace('recursos', items);
}

function diskState(d) {
  if (d.estado === 'ausente') return d.opcional ? ['unknown', 'Desconectado'] : ['warning', 'Desconectado'];
  if (d.estado === 'error') return ['warning', 'Ilegible'];
  if (d.salud === false || d.aviso_critico) return ['critical', 'FAILED'];
  if (d.pendientes || d.incorregibles || d.errores_medio) return ['critical', 'Sectores'];
  if (d.reasignados) return ['warning', 'Reasignados'];
  return ['ok', d.estado === 'standby' ? 'En reposo' : 'Sano'];
}

function renderSmart(status) {
  const u = status.umbrales;
  if (!status.smart.length) {
    replace('smart', empty('Sin datos SMART. ¿Está instalado host/smart/instalar.sh?'));
    return;
  }
  const rows = status.smart.map((d) => {
    const [level, text] = diskState(d);
    const limit = (d.protocolo || '').toLowerCase() === 'nvme' ? u.smart_temp_nvme_c : u.smart_temp_hdd_c;
    const facts = [];
    if (d.temp_c != null) facts.push(`${num(d.temp_c)} °C (umbral ${num(limit)})`);
    if (d.reasignados != null) {
      facts.push(`sectores: ${d.reasignados} reasignados · ${d.pendientes ?? 0} pendientes · ${d.incorregibles ?? 0} incorregibles`);
    }
    if (d.desgaste_pct != null) facts.push(`desgaste ${d.desgaste_pct} % · reserva ${d.reserva_pct ?? '—'} %`);
    if (d.errores_medio) facts.push(`${d.errores_medio} errores de medio`);
    if (d.horas != null) facts.push(`${num(d.horas)} h encendido`);
    if (d.error) facts.push(d.error);
    return h(
      'li',
      {},
      h(
        'div',
        { class: 'row-head' },
        h('span', { class: 'row-title' }, d.nombre),
        badge(level, text),
        h('span', { class: 'faint small' }, [d.montaje, d.modelo].filter(Boolean).join(' · ')),
      ),
      h('div', { class: 'row-facts' }, facts.map((f) => h('span', {}, f))),
      h(
        'div',
        { class: 'row-facts' },
        h('span', { class: 'faint small' }, 'comprobado ', timeEl(d.comprobado)),
        d.estado === 'standby' && d.leido
          ? h('span', { class: 'faint small' }, 'datos de ', timeEl(d.leido))
          : null,
      ),
    );
  });
  replace('smart', h('ul', { class: 'rows' }, rows));
}

function renderAccesos(status) {
  const a = status.accesos;
  const u = status.umbrales;
  if (!a) {
    replace('accesos', empty('Esperando la primera lectura…'));
    return;
  }
  const stats = h(
    'div',
    { class: 'stats' },
    stat(`Fallos (${num(u.fuerza_bruta_ventana_min)} min)`, num(a.fallos_ventana)),
    stat('IPs con fallos', num(a.ips_seguidas)),
    stat('Líneas leídas', num(a.lineas)),
  );
  const logState = h(
    'p',
    { class: 'small muted' },
    a.legible ? badge('ok', 'Legible') : badge('warning', 'Ilegible'),
    ` ${a.log} · fuerza bruta: más de ${u.fuerza_bruta} fallos de una IP · confianza: ${u.redes_confianza.join(', ')}`,
  );
  const logins = a.ultimos_accesos.length
    ? table(
        [['Cuándo', 'nowrap'], ['Usuario'], ['IP'], ['Método', 'hide-narrow'], ['Origen']],
        a.ultimos_accesos.map((l) =>
          h(
            'tr',
            {},
            h('td', {}, timeEl(l.fecha)),
            h('td', {}, l.usuario),
            h('td', {}, l.ip),
            h('td', {}, l.metodo),
            h('td', {}, l.confianza ? badge('ok', 'Confianza') : badge('critical', 'Externa')),
          ),
        ),
      )
    : empty('Ningún login aceptado desde que arrancó Cerbero.');
  replace('accesos', stats, logState, h('h3', { class: 'small muted' }, 'Últimos accesos aceptados'), logins);
}

function renderTareas(status) {
  if (!status.tareas.length) {
    replace('tareas', empty('Sin informes de tareas. ¿Está instalado host/tareas/instalar.sh?'));
    return;
  }
  const rows = status.tareas.map((t) => {
    const [level, text] =
      t.resultado !== 'ok' ? ['warning', 'Falló'] : t.parada ? ['warning', 'No se ejecuta'] : ['ok', 'Al día'];
    return h(
      'li',
      {},
      h('div', { class: 'row-head' }, h('span', { class: 'row-title' }, t.tarea), badge(level, text)),
      h(
        'div',
        { class: 'row-facts' },
        h('span', {}, t.resumen),
        h('span', { class: 'faint small' }, 'último informe ', timeEl(t.fin), t.cada_s ? ` · ${every(t.cada_s)}` : ''),
      ),
    );
  });
  replace('tareas', h('ul', { class: 'rows' }, rows));
}

function containerLevel(c) {
  if (c.status !== 'running') return c.status === 'restarting' ? 'critical' : 'warning';
  if (c.health === 'unhealthy') return 'warning';
  return 'ok';
}

function renderContenedores(status) {
  const open = document.querySelector('#contenedores details')?.open;
  const all = status.containers;
  if (!all.length) {
    replace('contenedores', empty('Sin contenedores (o Docker no responde).'));
    return;
  }
  const running = all.filter((c) => c.status === 'running').length;
  const unhealthy = all.filter((c) => c.health === 'unhealthy').length;
  const restarted = all.filter((c) => c.restarts > 0).length;
  const stats = h(
    'div',
    { class: 'stats' },
    stat('En marcha', `${running} / ${all.length}`),
    stat('No en marcha', num(all.length - running)),
    stat('Unhealthy', num(unhealthy)),
    stat('Con reinicios', num(restarted)),
  );
  const rank = { critical: 0, warning: 1, ok: 2 };
  const sorted = [...all].sort(
    (a, b) =>
      rank[containerLevel(a)] - rank[containerLevel(b)] ||
      b.restarts - a.restarts ||
      a.name.localeCompare(b.name),
  );
  const row = (c) => {
    const level = containerLevel(c);
    return h(
      'tr',
      {},
      h('td', {}, c.name),
      h('td', {}, badge(level, c.status === 'running' ? 'En marcha' : c.status)),
      h('td', {}, c.health || '—'),
      h('td', { class: 'num' }, num(c.restarts)),
    );
  };
  const headers = [['Contenedor'], ['Estado'], ['Health'], ['Reinicios', 'num']];
  const problems = sorted.filter((c) => containerLevel(c) !== 'ok' || c.restarts > 0);
  const parts = [stats];
  if (problems.length) {
    parts.push(h('h3', { class: 'small muted' }, 'Con problemas o reinicios'), table(headers, problems.map(row)));
  } else {
    parts.push(h('p', { class: 'small' }, badge('ok', 'Todo en orden'), ' Ningún contenedor caído, unhealthy ni con reinicios.'));
  }
  parts.push(
    h('details', { open }, h('summary', {}, `Ver los ${all.length} contenedores`), table(headers, sorted.map(row))),
    h('p', { class: 'small faint' }, 'CPU, RAM y logs de cada contenedor están en Dis.'),
  );
  replace('contenedores', parts);
}

// --- Tema y ciclo de refresco ---------------------------------------------------------

function setupTheme() {
  document.getElementById('theme').addEventListener('click', () => {
    const root = document.documentElement;
    const dark =
      root.dataset.theme === 'dark' ||
      (!root.dataset.theme && matchMedia('(prefers-color-scheme: dark)').matches);
    root.dataset.theme = dark ? 'light' : 'dark';
    try {
      localStorage.setItem('cerbero_theme', root.dataset.theme);
    } catch (e) {
      // Sin almacenamiento: el tema dura lo que la pestaña.
    }
  });
}

let timer = null;

function schedule() {
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(tick, REFRESH_MS);
}

async function tick() {
  await refresh();
  schedule();
}

document.addEventListener('visibilitychange', () => {
  // Con la pestaña oculta no se consulta nada; al volver, se actualiza ya.
  if (document.hidden) clearTimeout(timer);
  else tick();
});
setInterval(renderUpdated, 1000);
setupTheme();
tick();
