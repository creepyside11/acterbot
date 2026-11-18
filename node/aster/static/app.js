"use strict";

const $ = (id) => document.getElementById(id);
const tg = window.Telegram?.WebApp;
const ns = "http://www.w3.org/2000/svg";
const statuses = {
  queued: "В очереди", running: "В работе", done: "Готово",
  failed: "Ошибка", cancelled: "Остановлено",
};
const pages = {work: "Рабочая область", history: "История", detail: "Задача", settings: "Настройки"};
const examples = {
  web: "Изучи https://example.com, проверь, для чего нужна эта страница, и сохрани выводы в Markdown.",
  file: "Изучи приложенный документ. Выдели главное и сохрани краткое резюме в TXT.",
  image: "Опиши, что видно на приложенном фото, и выпиши читаемый текст.",
};

let token = getSavedToken();
let settings;
let parent = null;
let section;
let jobId;
let historyRows = [];
let detailSnapshot = "";
let recentSnapshot = "";
let busyRefresh = false;

function getSavedToken() {
  try { return sessionStorage.getItem("aster-session") || ""; } catch { return ""; }
}

function saveToken(value) {
  token = value;
  try {
    if (value) sessionStorage.setItem("aster-session", value);
    else sessionStorage.removeItem("aster-session");
  } catch {}
}

function showAuth(error = "") {
  $("shell").hidden = true;
  $("auth").hidden = false;
  $("login-error").textContent = error;
}

async function api(path, {method = "GET", data, form, unauthenticated = false} = {}) {
  const headers = {};
  if (!unauthenticated && token) headers.Authorization = "Bearer " + token;
  if (data) headers["Content-Type"] = "application/json";
  const response = await fetch("/api" + path, {
    method, headers, body: form || (data ? JSON.stringify(data) : undefined), cache: "no-store",
  });
  let result;
  try { result = await response.json(); } catch { throw new Error("Сервер вернул неожиданный ответ"); }
  if (!response.ok) {
    if (response.status === 401 && !unauthenticated) {
      saveToken("");
      showAuth("Сессия закончилась. Войдите снова.");
    }
    throw new Error(typeof result.detail === "string" ? result.detail : "Запрос не выполнен");
  }
  return result;
}

function node(tag, classes, text) {
  const el = document.createElement(tag);
  if (classes) el.className = classes;
  if (text !== undefined) el.textContent = text;
  return el;
}

function icon(name) {
  const el = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", "#" + name);
  el.setAttribute("aria-hidden", "true");
  el.append(use);
  return el;
}

function dateText(time, short = false) {
  const date = new Date(time);
  if (Number.isNaN(date.valueOf())) return "";
  return new Intl.DateTimeFormat("ru-RU", short
    ? {hour: "2-digit", minute: "2-digit", second: "2-digit"}
    : {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}).format(date);
}

function statusPill(status) {
  const element = node("span", "status", statuses[status] || status);
  element.dataset.status = status;
  return element;
}

function drawJobs(target, list) {
  if (!list.length) {
    target.replaceChildren(node("p", "empty", "Задач пока нет. Первая появится после запуска."));
    return;
  }
  target.replaceChildren(...list.map((job) => {
    const link = node("a", "job-row");
    link.href = "#job/" + job.id;
    const label = node("div", "job-label");
    label.append(node("strong", "", job.prompt), node("small", "", dateText(job.created)));
    const arrow = icon("arrow");
    arrow.classList.add("chevron");
    link.append(node("span", "job-number", "#" + job.id), label, statusPill(job.status), arrow);
    return link;
  }));
}

function makeBox(title, content) {
  const box = node("div", "box");
  box.append(node("h2", "", title), content);
  return box;
}

function button(label, action) {
  const el = node("button", "ghost", label);
  el.type = "button";
  el.addEventListener("click", () => action(el));
  return el;
}

async function downloadFile(file) {
  const {url} = await api("/files/" + file.id + "/link", {method: "POST"});
  const full = new URL(url, location.origin);
  if (tg?.initData && tg.isVersionAtLeast?.("8.0") && tg.downloadFile) {
    tg.downloadFile({url: full.href, file_name: file.name});
    return;
  }
  const link = node("a");
  link.href = full.href;
  link.download = file.name;
  document.body.append(link);
  link.click();
  link.remove();
}

function fileList(files) {
  const list = node("div", "file-list");
  for (const file of files) {
    const control = button(file.name, (el) => act(el, "detail-error", () => downloadFile(file)));
    control.prepend(icon("download"));
    control.title = Math.max(1, Math.ceil(file.bytes / 1024)) + " КБ";
    list.append(control);
  }
  return list;
}

function drawDetails(job) {
  const top = node("div", "detail-heading");
  top.append(node("h1", "", "Задача №" + job.id), statusPill(job.status));
  const meta = node("div", "detail-meta");
  meta.append(node("span", "hint", dateText(job.created)));
  if (["queued", "running"].includes(job.status)) {
    meta.append(button("Остановить", (el) => act(el, "detail-error", async () => {
      await api("/jobs/" + job.id + "/cancel", {method: "POST"});
      await refresh();
    })));
  } else {
    meta.append(button("Повторить", (el) => act(el, "detail-error", async () => {
      const result = await api("/jobs/" + job.id + "/retry", {method: "POST"});
      location.hash = "job/" + result.id;
    })));
    if (job.answer) meta.append(button("Продолжить", () => setParent(job.id)));
  }
  const content = [top, meta, makeBox("Задание", node("p", "body-text", job.prompt))];
  if (job.answer) content.push(makeBox("Ответ агента", node("p", "body-text", job.answer)));
  if (job.error) content.push(makeBox("Причина", node("p", "body-text", job.error)));
  if (job.plan) content.push(makeBox("План и заметки", node("p", "body-text", job.plan)));
  if (job.subagents?.length) {
    const opened = new Set(Array.from($("detail-content").querySelectorAll("details[open]"), (row) => row.dataset.subagent));
    const list = node("div", "subagent-list");
    for (const subagent of job.subagents) {
      const row = node("details", "subagent");
      row.dataset.subagent = subagent.ref;
      row.open = opened.has(subagent.ref);
      const label = node("summary", "subagent-label");
      label.append(node("strong", "", subagent.title), statusPill(subagent.status));
      row.append(label, node("p", "body-text", subagent.prompt));
      if (subagent.answer) row.append(node("p", "body-text subagent-answer", subagent.answer));
      list.append(row);
    }
    content.push(makeBox("Субагенты", list));
  }
  const outputs = job.files.filter(({kind}) => kind === "output");
  const inputs = job.files.filter(({kind}) => kind === "input");
  if (outputs.length) content.push(makeBox("Файлы результата", fileList(outputs)));
  if (inputs.length) content.push(makeBox("Вложения", fileList(inputs)));
  if (job.events.length) {
    const events = node("ol", "events");
    for (const event of job.events) {
      const item = node("li");
      item.append(node("time", "", dateText(event.time, true)), document.createTextNode(event.text));
      events.append(item);
    }
    content.push(makeBox("Журнал агента", events));
  }
  $("detail-content").replaceChildren(...content);
}

async function refresh() {
  if (busyRefresh || !token) return;
  busyRefresh = true;
  const here = section;
  const ident = jobId;
  try {
    if (here === "detail") {
      const job = await api("/jobs/" + ident);
      if (section !== here || jobId !== ident) return;
      const fingerprint = JSON.stringify(job);
      if (detailSnapshot !== fingerprint) {
        detailSnapshot = fingerprint;
        drawDetails(job);
      }
    } else if (here === "work") {
      const rows = (await api("/jobs")).slice(0, 5);
      if (section !== here) return;
      const fingerprint = JSON.stringify(rows);
      if (recentSnapshot !== fingerprint) {
        recentSnapshot = fingerprint;
        drawJobs($("recent-jobs"), rows);
      }
    }
  } catch (error) {
    if (here === "detail" && section === here && jobId === ident) $("detail-error").textContent = error.message;
    if (here === "work" && section === here) $("recent-jobs").replaceChildren(node("p", "empty", error.message));
  } finally {
    busyRefresh = false;
    if (here !== section || (here === "detail" && ident !== jobId)) queueMicrotask(refresh);
  }
}

function showModel(value) {
  settings = value;
  const label = value.model || "Модель не настроена";
  $("top-model").textContent = label;
  $("sidebar-model").textContent = label;
  $("server-dot").classList.toggle("offline", !value.model);
  $("top-model").previousElementSibling.classList.toggle("offline", !value.model);
  $("server-label").textContent = value.browser ? "VPS · Chromium" : "VPS";
  const form = $("settings-form").elements;
  for (const field of ["base_url", "model", "steps", "minutes", "subagents"]) form[field].value = value[field];
  for (const field of ["tools", "vision"]) form[field].checked = value[field];
  form.api_key.value = "";
  form.api_key.placeholder = value.has_key ? "Ключ сохранён" : "Ключ провайдера";
  form.clear_key.checked = false;
  $("clear-key-row").hidden = !value.has_key;
  $("workspace-status").textContent = value.workspace
    ? "Рабочая папка VPS подключена. В задании указывайте пути относительно /workspace."
    : "Рабочая папка VPS подключается при запуске через Docker Compose.";
}

function setParent(id) {
  parent = id;
  $("continuation").hidden = !id;
  $("continuation-text").textContent = id ? "Продолжение задачи №" + id : "";
  if (id) location.hash = "work";
}

async function loadHistory(more = false) {
  try {
    const before = more && historyRows.length ? "?before=" + historyRows.at(-1).id : "";
    const rows = await api("/jobs" + before);
    if (section !== "history") return;
    historyRows = more ? [...historyRows, ...rows] : rows;
    drawJobs($("history-jobs"), historyRows);
    $("load-more").hidden = rows.length < 80;
  } catch (error) {
    if (section === "history") $("history-jobs").replaceChildren(node("p", "empty", error.message));
  }
}

function route() {
  if (!token) return;
  const name = location.hash.slice(1);
  const match = /^job\/([1-9]\d{0,15})$/.exec(name);
  section = match ? "detail" : (["work", "history", "settings"].includes(name) ? name : "work");
  jobId = match?.[1];
  for (const id of Object.keys(pages)) $(id).hidden = id !== section;
  $("breadcrumb").textContent = pages[section];
  for (const link of document.querySelectorAll("[data-nav]")) {
    const active = (section === "detail" ? "history" : section) === link.dataset.nav;
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (section === "history") loadHistory();
  if (section === "detail") {
    $("detail-content").replaceChildren(node("p", "empty", "Загрузка…"));
    $("detail-error").textContent = "";
    detailSnapshot = "";
  }
  if (section === "work" && parent) $("job-prompt").focus();
  window.scrollTo(0, 0);
  refresh();
}

async function act(control, messageId, fn) {
  control.disabled = true;
  const message = $(messageId);
  message.textContent = "";
  try {
    await fn();
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  } finally {
    control.disabled = false;
  }
}

async function start() {
  showModel(await api("/settings"));
  $("auth").hidden = true;
  $("shell").hidden = false;
  route();
}

$("login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(event.submitter, "login-error", async () => {
    const {token: value} = await api("/login", {
      method: "POST", data: {password: $("password").value}, unauthenticated: true,
    });
    $("password").value = "";
    saveToken(value);
    await start();
  });
});

function logout() {
  saveToken("");
  showAuth();
}

$("logout").addEventListener("click", logout);
$("logout-mobile").addEventListener("click", logout);
$("clear-parent").addEventListener("click", () => setParent(null));
$("attach").addEventListener("click", () => $("job-files").click());
$("job-files").addEventListener("change", () => {
  const selection = Array.from($("job-files").files);
  const bad = selection.length > 4 || selection.some((file) => file.size > 8 * 1024 * 1024);
  if (bad) $("job-files").value = "";
  $("file-names").replaceChildren(...(bad ? [] : selection.map((file) => node("span", "tag", file.name))));
  $("job-error").textContent = bad ? "Можно приложить до 4 файлов, каждый до 8 МБ" : "";
});
for (const el of document.querySelectorAll("[data-example]")) {
  el.addEventListener("click", () => {
    $("job-prompt").value = examples[el.dataset.example];
    $("job-prompt").focus();
  });
}

$("job-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(event.submitter, "job-error", async () => {
    if (!settings.model) {
      location.hash = "settings";
      throw new Error("Укажите модель ИИ в настройках");
    }
    const form = new FormData(event.currentTarget || $("job-form"));
    if (!$("job-files").files.length) form.delete("files");
    if (parent) form.set("parent", parent);
    const {id} = await api("/jobs", {method: "POST", form});
    $("job-form").reset();
    $("file-names").replaceChildren();
    setParent(null);
    location.hash = "job/" + id;
  });
});

$("settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  act(event.submitter, "settings-message", async () => {
    const form = $("settings-form").elements;
    const body = {
      base_url: form.base_url.value, model: form.model.value,
      steps: Number(form.steps.value), minutes: Number(form.minutes.value),
      subagents: Number(form.subagents.value),
      tools: form.tools.checked, vision: form.vision.checked,
      api_key: form.clear_key.checked ? "" : (form.api_key.value.trim() || null),
    };
    showModel(await api("/settings", {method: "PUT", data: body}));
    $("settings-message").classList.remove("error");
    $("settings-message").textContent = "Настройки сохранены.";
  });
});
$("test-model").addEventListener("click", (event) => act(event.currentTarget, "settings-message", async () => {
  $("settings-message").classList.remove("error");
  $("settings-message").textContent = "Проверяем сохранённую конфигурацию…";
  const {reply} = await api("/settings/test", {method: "POST"});
  $("settings-message").textContent = "API ответил" + (reply ? " · " + reply : "");
}));
$("load-more").addEventListener("click", (event) => act(event.currentTarget, "detail-error", () => loadHistory(true)));
window.addEventListener("hashchange", route);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 2200);

(async () => {
  if (tg?.initData) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#13141b"); } catch {}
  }
  try {
    if (!token && tg?.initData) {
      const result = await api("/login", {
        method: "POST", data: {init_data: tg.initData}, unauthenticated: true,
      });
      saveToken(result.token);
    }
    if (token) await start();
    else showAuth();
  } catch (error) {
    saveToken("");
    showAuth(error.message);
  }
})();
