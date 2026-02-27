const state = {
  clients: [],
  activeClientId: null,
  activeClient: null,
};

const el = {
  clientName: document.getElementById("clientName"),
  pineconeNamespace: document.getElementById("pineconeNamespace"),
  createClientBtn: document.getElementById("createClientBtn"),
  clientList: document.getElementById("clientList"),
  activeClientName: document.getElementById("activeClientName"),
  activeClientId: document.getElementById("activeClientId"),
  activeClientCfg: document.getElementById("activeClientCfg"),
  authBadge: document.getElementById("authBadge"),
  refreshClientBtn: document.getElementById("refreshClientBtn"),
  oauthBtn: document.getElementById("oauthBtn"),
  linkInput: document.getElementById("linkInput"),
  addLinkBtn: document.getElementById("addLinkBtn"),
  linkSummary: document.getElementById("linkSummary"),
  airbyteConnectBtn: document.getElementById("airbyteConnectBtn"),
  syncAllBtn: document.getElementById("syncAllBtn"),
  syncStatus: document.getElementById("syncStatus"),
  docList: document.getElementById("docList"),
  questionInput: document.getElementById("questionInput"),
  askBtn: document.getElementById("askBtn"),
  answerBox: document.getElementById("answerBox"),
  retrievalBox: document.getElementById("retrievalBox"),
};

async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });

  const json = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(json.detail || JSON.stringify(json) || "Request failed");
  }
  return json;
}

function requireClient() {
  if (!state.activeClientId) {
    throw new Error("Select a client context first.");
  }
}

function setStatus(message) {
  el.syncStatus.textContent = message;
}

function renderClients() {
  el.clientList.innerHTML = "";
  if (!state.clients.length) {
    el.clientList.innerHTML = "<div class='muted'>No clients yet.</div>";
    return;
  }

  for (const client of state.clients) {
    const item = document.createElement("div");
    item.className = "client-item" + (client.client_id === state.activeClientId ? " active" : "");
    item.innerHTML = `
      <div class="client-name">${client.name}</div>
      <div class="client-meta">${client.client_id.slice(0, 8)}... | ns: ${client.pinecone_namespace || "-"} | docs: ${client.doc_count} | auth: ${client.has_auth ? "yes" : "no"}</div>
    `;
    item.addEventListener("click", () => selectClient(client.client_id));
    el.clientList.appendChild(item);
  }
}

function renderActiveClient() {
  const c = state.activeClient;
  if (!c) {
    el.activeClientName.textContent = "Select a client";
    el.activeClientId.textContent = "No context selected";
    el.activeClientCfg.textContent = "";
    el.authBadge.className = "badge danger";
    el.authBadge.textContent = "Not authenticated";
    el.docList.innerHTML = "<div class='muted'>No docs</div>";
    return;
  }

  el.activeClientName.textContent = c.name;
  el.activeClientId.textContent = c.client_id;
  el.activeClientCfg.textContent = `namespace=${c.pinecone_namespace || "-"}${c.airbyte_connection_id ? " | Airbyte connected" : ""}`;
  el.authBadge.className = c.has_auth ? "badge success" : "badge danger";
  el.authBadge.textContent = c.has_auth ? "Authenticated" : "Not authenticated";

  const folderLine = c.drive_folder_id ? `Folder: ${c.drive_folder_id}` : "No sync folder";
  const docCount = (c.docs && c.docs.length) ? c.docs.length : 0;
  if (el.linkSummary) el.linkSummary.textContent = `${folderLine} · ${docCount} doc(s)`;

  if (!c.docs || !c.docs.length) {
    el.docList.innerHTML = "<div class='muted'>No links yet. Paste folder or doc URL above.</div>";
    return;
  }

  el.docList.innerHTML = "";
  for (const doc of c.docs) {
    const item = document.createElement("div");
    item.className = "doc-item";
    item.innerHTML = `
      <div>
        <div><strong>${doc.doc_id}</strong></div>
        <div class="meta">${doc.url}</div>
      </div>
    `;
    el.docList.appendChild(item);
  }
}

async function loadClients() {
  state.clients = await api("/clients");
  renderClients();
}

async function selectClient(clientId) {
  state.activeClientId = clientId;
  await refreshActiveClient();
  renderClients();
}

async function refreshActiveClient() {
  if (!state.activeClientId) {
    state.activeClient = null;
    renderActiveClient();
    return;
  }
  state.activeClient = await api(`/clients/${state.activeClientId}`);
  renderActiveClient();
}

async function createClient() {
  const name = el.clientName.value.trim();
  const pineconeNamespace = el.pineconeNamespace.value.trim();

  if (!name || !pineconeNamespace) {
    throw new Error("Name and Pinecone namespace are required.");
  }

  const created = await api("/clients", "POST", {
    name,
    pinecone_namespace: pineconeNamespace,
  });

  el.clientName.value = "";
  el.pineconeNamespace.value = "";

  await loadClients();
  await selectClient(created.client_id);
}

async function startOAuth() {
  requireClient();
  const result = await api("/oauth/init", "POST", { client_id: state.activeClientId });
  const popup = window.open(result.authorization_url, "google_oauth", "width=540,height=700");
  if (!popup) throw new Error("Popup blocked. Allow popups and retry.");
  setStatus("OAuth started. Complete consent in popup.");

  const maxWaitMs = 120000;
  const intervalMs = 2000;
  let elapsed = 0;

  const timer = setInterval(async () => {
    elapsed += intervalMs;
    try {
      await refreshActiveClient();
      await loadClients();
      if (state.activeClient?.has_auth) {
        setStatus("OAuth success. Client authenticated.");
        clearInterval(timer);
      }
      if (elapsed >= maxWaitMs) {
        clearInterval(timer);
        setStatus("OAuth polling timeout. Click Refresh.");
      }
    } catch (_) {}
  }, intervalMs);
}

async function addLink() {
  requireClient();
  const url = el.linkInput?.value?.trim();
  if (!url) return;
  const result = await api("/links/add", "POST", {
    client_id: state.activeClientId,
    url,
  });
  if (result.type === "folder") {
    setStatus(`Sync folder set: ${result.folder_id}`);
  } else {
    setStatus(`Doc registered: ${result.doc_id}`);
  }
  if (el.linkInput) el.linkInput.value = "";
  await refreshActiveClient();
  await loadClients();
}

async function airbyteConnect() {
  requireClient();
  setStatus("Connecting to Airbyte...");
  const result = await api("/airbyte/connect", "POST", { client_id: state.activeClientId });
  setStatus(JSON.stringify(result));
  await refreshActiveClient();
  await loadClients();
}

async function syncAll() {
  requireClient();
  setStatus("Triggering Airbyte sync...");
  const result = await api("/airbyte/trigger-sync", "POST", {
    client_id: state.activeClientId,
  });
  setStatus(JSON.stringify(result));
  await refreshActiveClient();
  await loadClients();
}

async function askRag() {
  requireClient();
  const question = el.questionInput.value.trim();
  if (!question) return;

  el.answerBox.textContent = "Thinking...";
  el.retrievalBox.textContent = "";

  const result = await api("/rag/chat", "POST", {
    client_id: state.activeClientId,
    question,
    top_k: 5,
  });

  el.answerBox.textContent = result.answer || "No answer returned.";

  const retrieval = (result.matches || []).map((m, idx) => {
    return `#${idx + 1} doc=${m.doc_id} chunk=${m.chunk_index} score=${Number(m.score || 0).toFixed(4)}\n${m.text || ""}`;
  });

  el.retrievalBox.textContent = retrieval.join("\n\n----------------\n\n") || "No retrieval matches.";
}

function bind() {
  el.createClientBtn.addEventListener("click", () => createClient().catch((e) => setStatus(e.message)));
  el.refreshClientBtn.addEventListener("click", () => refreshActiveClient().catch((e) => setStatus(e.message)));
  el.oauthBtn.addEventListener("click", () => startOAuth().catch((e) => setStatus(e.message)));
  el.addLinkBtn?.addEventListener("click", () => addLink().catch((e) => setStatus(e.message)));
  el.airbyteConnectBtn?.addEventListener("click", () => airbyteConnect().catch((e) => setStatus(e.message)));
  el.syncAllBtn.addEventListener("click", () => syncAll().catch((e) => setStatus(e.message)));
  el.askBtn.addEventListener("click", () => askRag().catch((e) => {
    el.answerBox.textContent = `Error: ${e.message}`;
  }));

  el.questionInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      askRag().catch((err) => {
        el.answerBox.textContent = `Error: ${err.message}`;
      });
    }
  });

  window.addEventListener("message", async (event) => {
    const data = event.data || {};
    if (data.type === "oauth-success" && data.clientId === state.activeClientId) {
      setStatus("OAuth callback received. Refreshing client state...");
      await refreshActiveClient();
      await loadClients();
      setStatus("OAuth success. Client authenticated.");
    }
  });
}

async function init() {
  bind();
  await loadClients();
  renderActiveClient();
  setStatus("Ready.");
}

init().catch((err) => {
  setStatus(`Init failed: ${err.message}`);
});
