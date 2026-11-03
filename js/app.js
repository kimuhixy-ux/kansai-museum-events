const PERIOD_OPTIONS = [['all', 'すべて'], ['ongoing', '開催中'], ['upcoming', 'これから']];
const KIND_OPTIONS = [['all', 'すべて'], ['exhibition', '展覧会'], ['event', 'イベント']];
const AREA_OPTIONS = ['すべて', '大阪', '京都', '兵庫', '奈良', '滋賀', '和歌山'];
const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];
const CALENDAR_BASE_URL = 'https://calendar.google.com/calendar/render';

const state = { events: [], museums: new Map(), period: 'all', kind: 'all', area: 'すべて', query: '' };

// toISOString は UTC になり、日本の午前中に日付が1日ずれるため手で組み立てる
function todayIso() {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${now.getFullYear()}-${month}-${day}`;
}

function parseIso(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  return new Date(year, month - 1, day);
}

function formatDate(iso, withYear) {
  const date = parseIso(iso);
  const yearText = withYear ? `${date.getFullYear()}年` : '';
  return `${yearText}${date.getMonth() + 1}月${date.getDate()}日(${WEEKDAYS[date.getDay()]})`;
}

function formatPeriod(event) {
  if (event.start === event.end) {
    const time = event.startTime ? ` ${event.startTime}〜${event.endTime}` : '';
    return formatDate(event.start, true) + time;
  }
  const sameYear = event.start.slice(0, 4) === event.end.slice(0, 4);
  return `${formatDate(event.start, true)}〜${formatDate(event.end, !sameYear)}`;
}

function compactDate(iso) {
  return iso.replaceAll('-', '');
}

// Google カレンダーの終日予定は終了日を「翌日」で指定する決まりになっている
function nextDayCompact(iso) {
  const date = parseIso(iso);
  date.setDate(date.getDate() + 1);
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${date.getFullYear()}${month}${day}`;
}

function calendarDates(event) {
  if (event.startTime && event.endTime) {
    const day = compactDate(event.start);
    return `${day}T${event.startTime.replace(':', '')}00/${day}T${event.endTime.replace(':', '')}00`;
  }
  return `${compactDate(event.start)}/${nextDayCompact(event.end)}`;
}

function buildCalendarUrl(event, museum) {
  const params = new URLSearchParams({
    action: 'TEMPLATE',
    text: event.title,
    dates: calendarDates(event),
    details: `${museum.name}\n公式ページ: ${event.url}\n※日時・開館日は公式ページでご確認ください。`,
    ctz: 'Asia/Tokyo',
  });
  // 館外開催を館の住所へ案内してしまわないよう、場所を入れない
  if (!/館外/.test(event.title)) {
    params.set('location', museum.name);
  }
  return `${CALENDAR_BASE_URL}?${params}`;
}

function isSafeUrl(url) {
  return /^https?:\/\//.test(url);
}

function matchesFilters(event, today) {
  const museum = state.museums.get(event.museumId);
  if (!museum || event.end < today) return false;
  if (state.kind !== 'all' && event.kind !== state.kind) return false;
  if (state.area !== 'すべて' && museum.area !== state.area) return false;
  if (state.period === 'ongoing' && event.start > today) return false;
  if (state.period === 'upcoming' && event.start <= today) return false;
  const haystack = `${event.title} ${museum.name}`.toLowerCase();
  return haystack.includes(state.query);
}

function createLink(className, text, href, label) {
  const link = document.createElement('a');
  link.className = className;
  link.textContent = text;
  link.href = href;
  link.target = '_blank';
  link.rel = 'noopener';
  link.setAttribute('aria-label', label);
  return link;
}

function createEventCard(event) {
  const museum = state.museums.get(event.museumId);
  const card = document.createElement('li');
  card.className = 'event-card';

  const kind = document.createElement('span');
  kind.className = event.kind === 'event' ? 'event-kind is-event' : 'event-kind';
  kind.textContent = event.kind === 'event' ? 'イベント' : '展覧会';

  const title = document.createElement('h2');
  title.className = 'event-title';
  title.textContent = event.title;

  const period = document.createElement('p');
  period.className = 'event-meta';
  period.textContent = formatPeriod(event);

  const place = document.createElement('p');
  place.className = 'event-meta';
  place.textContent = `${museum.name}（${museum.area}）`;

  const actions = document.createElement('div');
  actions.className = 'event-actions';
  actions.append(createLink('button primary', 'カレンダーに追加', buildCalendarUrl(event, museum), `${event.title}をGoogleカレンダーに追加`));
  if (isSafeUrl(event.url)) {
    actions.append(createLink('button', '公式ページ', event.url, `${event.title}の公式ページ`));
  }
  card.append(kind, title, period, place, actions);
  return card;
}

function renderEvents() {
  const today = todayIso();
  const visible = state.events.filter((event) => matchesFilters(event, today));
  document.getElementById('event-list').replaceChildren(...visible.map(createEventCard));
  document.getElementById('summary').textContent = visible.length
    ? `${visible.length}件`
    : '条件に合う催しはありません。';
}

function renderChips(containerId, options, stateKey) {
  const container = document.getElementById(containerId);
  const buttons = options.map(([value, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'chip';
    button.textContent = label;
    button.setAttribute('aria-pressed', String(state[stateKey] === value));
    button.addEventListener('click', () => {
      state[stateKey] = value;
      buttons.forEach((other) => other.setAttribute('aria-pressed', String(other === button)));
      renderEvents();
    });
    return button;
  });
  container.replaceChildren(...buttons);
}

function renderManualMuseums(museums, statuses) {
  const manual = museums.filter((museum) => {
    const status = statuses[museum.id];
    return !museum.pages.length || !status || status.errors.length > 0 || status.count === 0;
  });
  const items = manual.filter((museum) => isSafeUrl(museum.site)).map((museum) => {
    const item = document.createElement('li');
    item.append(createLink('', `${museum.name}（${museum.area}）`, museum.site, `${museum.name}の公式サイト`));
    return item;
  });
  document.getElementById('manual-list').replaceChildren(...items);
  document.getElementById('manual-section').hidden = items.length === 0;
}

async function fetchJson(path) {
  const response = await fetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

async function init() {
  renderChips('period-filter', PERIOD_OPTIONS, 'period');
  renderChips('kind-filter', KIND_OPTIONS, 'kind');
  renderChips('area-filter', AREA_OPTIONS.map((area) => [area, area]), 'area');
  document.getElementById('search').addEventListener('input', (inputEvent) => {
    state.query = inputEvent.target.value.trim().toLowerCase();
    renderEvents();
  });
  try {
    const [museums, data] = await Promise.all([fetchJson('data/museums.json'), fetchJson('data/events.json')]);
    state.museums = new Map(museums.map((museum) => [museum.id, museum]));
    state.events = data.events;
    renderEvents();
    renderManualMuseums(museums, data.statuses);
    document.getElementById('updated').textContent = `情報の更新日: ${formatDate(data.updatedAt, true)}`;
  } catch (error) {
    document.getElementById('summary').textContent = 'データを読み込めませんでした。通信状況を確認して、もう一度開いてください。';
    console.error(error);
  }
}

init();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch((error) => console.error(error));
}
