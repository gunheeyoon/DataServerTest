const byId = (id) => document.getElementById(id);
const connectButton = byId("connect");
const commandInput = byId("command");
const readField = byId("read-field");
const subGroup = byId("sub-group");
const writeField = byId("write-field");
const writeValue = byId("write-value");
const publishGroup = byId("publish-group");
const publishValues = byId("publish-values");
const streamCards = new Map();
const publicationCards = new Map();
const fieldSearches = [readField, subGroup, writeField, publishGroup];
const searchState = new Map(fieldSearches.map((input) => [input, {
  list: byId(input.getAttribute("aria-controls")),
  placeholder: input.placeholder,
  entries: [],
  matches: [],
  active: -1,
}]));

let session = null;
let busy = false;
let revision = 0;
let streamCount = 0;
let pollTimer;

async function api(path, body) {
  const headers = session ? { "X-Session": session.session } : {};
  const options = { headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    Object.assign(options, { method: "POST", body: JSON.stringify(body) });
  }
  const response = await fetch(`/api/${path}`, options);
  const result = await response.json();
  if (!response.ok) {
    throw Object.assign(new Error(result.error), { status: response.status });
  }
  return result;
}

function notice(message = "") {
  byId("notice").textContent = message;
  byId("notice").hidden = !message;
}

function updateControls() {
  const online = Boolean(session);
  for (const control of document.querySelectorAll("[data-online]")) {
    control.disabled = !online || busy;
  }
  if (online) {
    for (const search of fieldSearches) {
      if (!searchState.get(search).entries.length) {
        for (const control of search.form.elements) control.disabled = true;
      }
    }
  }
  for (const search of fieldSearches) {
    if (search.disabled) closeSuggestions(search);
  }
  byId("stop-all").disabled = !online || busy || !streamCount;
  for (const card of streamCards.values()) card.stop.disabled = busy;
  for (const card of publicationCards.values()) {
    card.stop.disabled = busy;
    card.input.disabled = busy;
    card.submit.disabled = busy;
    card.pause.disabled = busy;
  }
  connectButton.disabled = busy;
  connectButton.textContent = online ? "Disconnect" : "Connect";
  byId("status").classList.toggle("online", online);
  byId("status").lastElementChild.textContent = online ? session.target : "Disconnected";
  byId("udp-port").textContent = online ? `UDP receive port ${session.udp_port}` : "UDP port assigned on connect";
  byId("catalog-empty").hidden = online;
  byId("catalog-content").hidden = !online;
}

function fillSuggestions(search, entries) {
  const state = searchState.get(search);
  state.entries = entries;
  closeSuggestions(search);
  search.value = "";
  search.setCustomValidity("");
  search.placeholder = entries.length ? state.placeholder : "No eligible matches";
}

function closeSuggestions(search) {
  const state = searchState.get(search);
  state.list.hidden = true;
  state.active = -1;
  search.setAttribute("aria-expanded", "false");
  search.removeAttribute("aria-activedescendant");
}

function chooseSuggestion(search, name) {
  search.value = name;
  search.setCustomValidity("");
  closeSuggestions(search);
}

function showSuggestions(search) {
  const state = searchState.get(search);
  const query = search.value.trim().toLowerCase();
  const matches = state.entries.filter(([name]) => name.toLowerCase().includes(query));
  // Put prefix matches first, while keeping catalog order within each set.
  state.matches = [
    ...matches.filter(([name]) => name.toLowerCase().startsWith(query)),
    ...matches.filter(([name]) => !name.toLowerCase().startsWith(query)),
  ];
  closeSuggestions(search);
  state.list.replaceChildren(...state.matches.map(([name, detail], index) => {
    const option = element("li");
    option.id = `${state.list.id}-${index}`;
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", "false");
    option.append(element("strong", name), element("small", detail));
    // Keep focus in the combobox until the choice is applied.
    option.addEventListener("mousedown", (event) => event.preventDefault());
    option.addEventListener("click", () => chooseSuggestion(search, name));
    return option;
  }));
  if (!state.matches.length) return;
  state.list.scrollTop = 0;
  state.list.hidden = false;
  search.setAttribute("aria-expanded", "true");
}

function moveSuggestion(search, step) {
  const state = searchState.get(search);
  if (state.list.hidden) showSuggestions(search);
  const options = state.list.children;
  if (!options.length) return;
  if (state.active >= 0) options[state.active].setAttribute("aria-selected", "false");
  const start = state.active < 0 && step < 0 ? 0 : state.active;
  state.active = (start + step + options.length) % options.length;
  const active = options[state.active];
  active.setAttribute("aria-selected", "true");
  search.setAttribute("aria-activedescendant", active.id);
  active.scrollIntoView({ block: "nearest" });
}

function renderCatalog(fields, groups) {
  const row = (name, detail) => {
    const item = element("li");
    item.append(element("strong", name), element("small", detail));
    return item;
  };
  byId("catalog-fields").replaceChildren(...Array.from(fields, ([name, field]) => {
    const permissions = [
      ["R", "Read"], ["W", "Write"], ["S", "Stream"],
    ].filter(([flag]) => field.flags.includes(flag)).map(([, label]) => label);
    return row(name, `${field.type} · ${permissions.join(", ") || "No client access"}`);
  }));
  byId("catalog-groups").replaceChildren(...groups.map(([name, members]) => row(name, members.join(", "))));
  byId("field-count").textContent = fields.size;
  byId("group-count").textContent = groups.length;
}

function applySchema(line, groups) {
  const fields = new Map(line.split(" ").slice(1).map((entry) => {
    const [name, type, flags] = entry.split(":");
    return [name, { type, flags }];
  }));
  const groupEntries = Object.entries(groups);
  fillSuggestions(readField, [
    ...groupEntries.filter(([name, members]) =>
      !fields.has(name) && members.every((member) => fields.get(member)?.flags.includes("R"))
    ).map(([name, members]) => [name, `Group · ${members.join(", ")}`]),
    ...[...fields].filter(([, field]) => field.flags.includes("R")).map(([name, field]) => [name, field.type]),
  ]);
  fillSuggestions(subGroup, groupEntries
    .filter(([, members]) => members.every((member) => fields.get(member)?.flags.includes("S")))
    .map(([name, members]) => [name, members.join(", ")]));
  fillSuggestions(writeField, [...fields]
    .filter(([, field]) => field.flags.includes("W"))
    .map(([name, field]) => [name, field.type]));
  fillSuggestions(publishGroup, groupEntries
    .filter(([, members]) => members.every((member) => fields.get(member)?.flags.includes("W")))
    .map(([name, members]) => [name, members.join(", ")]));
  renderCatalog(fields, groupEntries);
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function logReply(command, reply) {
  const list = byId("replies");
  const row = element("article", undefined, reply.startsWith("ERR") ? "reply error" : "reply");
  const timestamp = new Date();
  const time = element("time", timestamp.toLocaleTimeString());
  time.dateTime = timestamp.toISOString();
  row.append(time, element("code", `  › ${command}`), element("pre", reply));
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 32;
  list.append(row);
  while (list.childElementCount > 100) list.firstElementChild.remove();
  if (atBottom) list.scrollTop = list.scrollHeight;
  byId("empty-replies").hidden = true;
}

function renderStreams(streams) {
  const activeNames = new Set(streams.map(({ name }) => name));
  for (const [name, card] of streamCards) {
    if (!activeNames.has(name)) {
      card.row.remove();
      streamCards.delete(name);
    }
  }
  for (const stream of streams) {
    let card = streamCards.get(stream.name);
    if (!card) {
      const row = element("article", undefined, "stream-card");
      const header = element("div", undefined, "stream-header");
      const title = element("div");
      const meta = element("span", "", "stream-meta");
      const packetCount = element("span", "", "stream-packets");
      const stop = element("button", "Stop");
      const values = element("dl", undefined, "values");
      stop.setAttribute("aria-label", `Stop ${stream.name} subscription`);
      stop.addEventListener("click", () => send(`UNSUB ${stream.name}`));
      title.append(element("h3", stream.name), meta, packetCount);
      header.append(title, stop);
      row.append(header, values);
      byId("streams").append(row);
      card = { row, meta, packetCount, values, stop, sample: null };
      streamCards.set(stream.name, card);
    }
    const status = stream.sequence === null ? "Waiting for data" :
      stream.age_ms > 1000 ? "No recent update" : "Receiving";
    const sequence = stream.sequence === null ? "" : ` · #${stream.sequence}`;
    card.meta.textContent = `${status} · UDP ${stream.port}${sequence}`;
    const packets = stream.packets_last_10s;
    const packetText = packets === undefined ? "Restart the bridge to show packet counts." :
      packets === null ? "Counting packets in the first 10 seconds…" :
      `${packets.toLocaleString()} packets received in the last 10-second window`;
    if (card.packetCount.textContent !== packetText) card.packetCount.textContent = packetText;
    const sample = stream.values.join(" ");
    if (sample !== card.sample) {
      card.values.replaceChildren(...stream.values.map((pair) => {
        const split = pair.indexOf("=");
        const cell = element("div", undefined, "value");
        cell.append(element("dt", pair.slice(0, split)), element("dd", pair.slice(split + 1)));
        return cell;
      }));
      card.sample = sample;
    }
    card.stop.disabled = busy;
  }
  streamCount = streams.length;
  byId("count").textContent = streamCount;
  byId("empty-streams").hidden = streamCount > 0;
  byId("stop-all").disabled = busy || !streamCount;
}

function renderPublications(publications) {
  const names = new Set(publications.map(({ name }) => name));
  for (const [name, card] of publicationCards) {
    if (!names.has(name)) {
      card.row.remove();
      publicationCards.delete(name);
    }
  }
  for (const publication of publications) {
    const { name, port, last_sent: lastSent, accepted_last_10s: accepted } = publication;
    let card = publicationCards.get(name);
    if (!card) {
      const row = element("article", undefined, "stream-card");
      const header = element("div", undefined, "stream-header");
      const title = element("div");
      const meta = element("span", undefined, "stream-meta");
      const packetCount = element("span", "", "stream-packets");
      const stop = element("button", "Unpublish");
      stop.setAttribute("aria-label", `Unpublish ${name}`);
      stop.addEventListener("click", () => send(`UNPUBLISH ${name}`));
      title.append(element("h3", name), meta, packetCount);
      header.append(title, stop);

      const form = element("form", undefined, "sample-form");
      const actions = element("div", undefined, "input-row sample-actions");
      const input = element("input");
      input.setAttribute("aria-label", `Sample values for ${name}`);
      input.required = true;
      input.autocomplete = "off";
      input.spellcheck = false;
      const fields = Object.hasOwn(session.groups, name) ? session.groups[name] : [];
      input.placeholder = fields.length ? fields.map((field) => `${field}=…`).join(" ") : "name=value";
      input.value = publication.sending_values ?? "";
      const submit = element("button", "Start 60 Hz");
      const pause = element("button", "Pause");
      pause.type = "button";
      pause.setAttribute("aria-label", `Pause sending ${name}`);
      pause.addEventListener("click", () => configurePublication(name, null));
      actions.append(input, submit, pause);
      form.append(actions);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        configurePublication(name, input.value);
      });
      row.append(header, form);
      byId("publications").append(row);
      card = { row, meta, packetCount, stop, input, submit, pause };
      publicationCards.set(name, card);
    }
    const activity = publication.send_error ? `UDP send error: ${publication.send_error}` :
      publication.sending ? "Sending · target 60 Hz" : "Reserved · paused";
    const status = `${activity} · UDP ${port} · Sent ${lastSent}`;
    if (card.meta.textContent !== status) card.meta.textContent = status;
    const submitLabel = publication.sending ? "Apply values" : "Start 60 Hz";
    if (card.submit.textContent !== submitLabel) card.submit.textContent = submitLabel;
    card.pause.hidden = !publication.sending;
    const packetText = accepted === undefined ? "Restart the bridge to show accepted packets." :
      publication.acceptance_supported === false ? "The connected server does not report accepted packets." :
      accepted === null ? "Counting accepted packets in the first 10 seconds…" :
      `${accepted.toLocaleString()} packets accepted in the last 10-second window`;
    if (card.packetCount.textContent !== packetText) card.packetCount.textContent = packetText;
    card.stop.disabled = busy;
    card.input.disabled = busy;
    card.submit.disabled = busy;
    card.pause.disabled = busy;
  }
  byId("publication-count").textContent = publications.length;
  byId("empty-publications").hidden = publications.length > 0;
  byId("sample-note").hidden = publications.length === 0;
}

function renderStatus(result) {
  renderStreams(result.streams);
  renderPublications(result.publications);
}

function clearSession() {
  session = null;
  revision++;
  clearTimeout(pollTimer);
  renderStatus({ streams: [], publications: [] });
}

async function poll() {
  const current = session;
  const version = revision;
  if (!current) return;
  try {
    const result = await api("status");
    if (session === current && revision === version) renderStatus(result);
  } catch (error) {
    if (session !== current) return;
    if (error.status === 404 || error.status === 502) clearSession();
    notice(error.message);
    updateControls();
  } finally {
    if (session === current) pollTimer = setTimeout(poll, 120);
  }
}

connectButton.addEventListener("click", async () => {
  busy = true;
  updateControls();
  notice();
  try {
    if (session) {
      try {
        await api("disconnect", {});
      } finally {
        clearSession();
      }
    } else {
      session = await api("connect", {});
      applySchema(session.schema, session.groups);
      poll();
    }
  } catch (error) {
    notice(error.message);
  } finally {
    busy = false;
    updateControls();
  }
});

async function send(command, path = "command", body = { command }) {
  if (!session || busy || !command.trim()) return false;
  busy = true;
  revision++;
  updateControls();
  notice();
  try {
    const result = await api(path, body);
    revision++;
    logReply(command, result.reply);
    renderStatus(result);
    return true;
  } catch (error) {
    if (error.status === 404 || error.status === 502) clearSession();
    logReply(command, `ERR ${error.message}`);
    return false;
  } finally {
    busy = false;
    updateControls();
  }
}

async function configurePublication(name, values) {
  if (!session || busy) return;
  busy = true;
  revision++;
  updateControls();
  notice();
  try {
    const result = await api("publish-stream", { name, values });
    revision++;
    renderStatus(result);
  } catch (error) {
    if (error.status === 404) clearSession();
    notice(error.message);
  } finally {
    busy = false;
    updateControls();
  }
}

for (const button of document.querySelectorAll("[data-command]")) {
  button.addEventListener("click", () => send(button.dataset.command));
}
for (const search of fieldSearches) {
  search.addEventListener("focus", () => showSuggestions(search));
  search.addEventListener("blur", () => closeSuggestions(search));
  search.addEventListener("input", () => {
    search.setCustomValidity("");
    showSuggestions(search);
  });
  search.addEventListener("keydown", (event) => {
    const state = searchState.get(search);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      moveSuggestion(search, event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Escape" && !state.list.hidden) {
      event.preventDefault();
      closeSuggestions(search);
    } else if (event.key === "Enter" && !state.list.hidden) {
      const option = state.matches[state.active] ??
        (state.matches.length === 1 && state.matches[0][0] !== search.value.trim() ? state.matches[0] : null);
      if (option) {
        event.preventDefault();
        chooseSuggestion(search, option[0]);
      }
    }
  });
}

function selectedName(search) {
  const name = search.value.trim();
  if (searchState.get(search).entries.some(([candidate]) => candidate === name)) return name;
  search.setCustomValidity("Choose an eligible name from the suggestions.");
  search.reportValidity();
  return null;
}

byId("read-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = selectedName(readField);
  if (name) send(`GET ${name}`);
});
byId("sub-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = selectedName(subGroup);
  if (session && name) send(`SUB ${session.udp_port} ${name}`);
});
byId("write-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = selectedName(writeField);
  if (name) send(`SET ${name}=${writeValue.value.trim()}`);
});
byId("publish-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = selectedName(publishGroup);
  if (name) send(`PUBLISH ${name}`, "publish", { name, values: publishValues.value });
});
byId("command-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (await send(commandInput.value)) commandInput.value = "";
  if (!commandInput.disabled) commandInput.focus();
});
byId("stop-all").addEventListener("click", () => send("SUB false"));
byId("clear").addEventListener("click", () => {
  byId("replies").replaceChildren();
  byId("empty-replies").hidden = false;
});
window.addEventListener("pagehide", () => {
  if (session) {
    fetch("/api/disconnect", {
      method: "POST", keepalive: true, body: "{}",
      headers: { "X-Session": session.session, "Content-Type": "application/json" },
    }).catch(() => {});
    clearSession();
    updateControls();
  }
});
