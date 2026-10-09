document.addEventListener('DOMContentLoaded', () => {

    // ── UI refs ─────────────────────────────────────────────────────────────
    const connType        = document.getElementById('connType');
    const stdioFields     = document.getElementById('stdioFields');
    const sseFields       = document.getElementById('sseFields');
    const logs            = document.getElementById('logContainer');
    const clearLogs       = document.getElementById('clearLogs');
    const connectBtn      = document.getElementById('connectBtn');
    const contentArea     = document.getElementById('contentArea');
    const placeholderState= document.getElementById('placeholderState');

    // Connection strip
    const connStrip       = document.getElementById('connStrip');
    const connStripItems  = document.getElementById('connStripItems');
    const newConnBtn      = document.getElementById('newConnBtn');

    // Register banner
    const registerBanner  = document.getElementById('registerBanner');
    const registerBannerYes=document.getElementById('registerBannerYes');
    const registerBannerNo = document.getElementById('registerBannerNo');

    // Server registry
    const serverList      = document.getElementById('serverList');
    const addServerBtn    = document.getElementById('addServerBtn');
    const addServerModal  = document.getElementById('addServerModal');
    const cancelAddServer = document.getElementById('cancelAddServer');
    const confirmAddServer= document.getElementById('confirmAddServer');
    const refreshAllBtn   = document.getElementById('refreshAllBtn');

    // Tool search
    const toolSearchInput = document.getElementById('toolSearchInput');
    const toolSearchBtn   = document.getElementById('toolSearchBtn');
    const searchResults   = document.getElementById('searchResults');

    // Tool execution modal
    const toolExecModal    = document.getElementById('toolExecModal');
    const execToolName     = document.getElementById('execToolName');
    const execToolDesc     = document.getElementById('execToolDesc');
    const execConnBadge    = document.getElementById('execConnBadge');
    const execRestId       = document.getElementById('execRestId');
    const execToolForm     = document.getElementById('execToolForm');
    const execToolJson     = document.getElementById('execToolJson');
    const execResultArea   = document.getElementById('execResultArea');
    const execResultStatus = document.getElementById('execResultStatus');
    const execResultContent= document.getElementById('execResultContent');
    const runToolBtn       = document.getElementById('runToolBtn');
    const closeExecModal   = document.getElementById('closeExecModal');
    const cancelExecModal  = document.getElementById('cancelExecModal');

    // ── Multi-connection state ───────────────────────────────────────────────
    /**
     * sessions: Map<connection_id, SessionEntry>
     *
     * SessionEntry {
     *   id: string
     *   target: string           URL or command
     *   type: 'http'|'stdio'
     *   name: string             display name
     *   status: 'connecting'|'active'|'closed'|'error'
     *   data: { tools, resources, prompts } | null
     *   registeredServerId: string | null
     *   bannerDismissed: boolean
     *   userInitiated: boolean   true when user explicitly clicked ×
     * }
     */
    const sessions = new Map();
    let selectedId = null;   // currently viewed session

    // Registered server list cached for "already registered?" check
    let registeredServers = [];
    // Active tab (shared across sessions)
    const tabApi = {
        active: 'tools',
        setTab(t) {
            document.querySelectorAll('.tab-btn').forEach(btn => {
                const on = btn.dataset.tab === t;
                btn.classList.toggle('text-indigo-600', on);
                btn.classList.toggle('border-indigo-600', on);
                btn.classList.toggle('text-gray-500', !on);
                btn.classList.toggle('border-transparent', !on);
            });
            tabApi.active = t;
            renderContent();
        }
    };

    // Quick-connect debounce guard
    let isConnecting = false;

    // Which session is currently awaiting a register-banner "Register" click
    let _bannerRegisterSessionId = null;
    // Tool currently open in the execute modal
    let _execTool = null;

    // ── Socket.IO ────────────────────────────────────────────────────────────
    const socket = io();

    socket.on('connect', () => {
        addLog('Connected to inspector server', 'success');
        document.getElementById('statusDot').classList.replace('bg-gray-400', 'bg-green-500');
        document.getElementById('statusText').innerText = 'Online';
    });

    socket.on('disconnect', () => {
        addLog('Disconnected from inspector server', 'error');
        document.getElementById('statusDot').classList.replace('bg-green-500', 'bg-gray-400');
        document.getElementById('statusText').innerText = 'Offline';
        isConnecting = false;
        connectBtn.disabled = false;
        // Mark all active sessions as closed
        for (const sess of sessions.values()) {
            if (sess.status === 'connecting' || sess.status === 'active') {
                sess.status = 'closed';
            }
        }
        renderConnStrip();
        renderContent();
    });

    socket.on('log', (data) => addLog(data.message));

    socket.on('connection_success', (data) => {
        isConnecting = false;
        connectBtn.disabled = false;
        const sess = sessions.get(data.id);
        if (!sess) return;
        sess.status = 'active';
        sess.data = data.data;
        addLog(`Connected: ${sess.name}`, 'success');
        // Auto-select if this is the only session or nothing is selected
        if (!selectedId || !sessions.has(selectedId)) selectSession(data.id);
        else renderConnStrip();   // just refresh badge colours
        renderContent();
    });

    socket.on('connection_error', (data) => {
        isConnecting = false;
        connectBtn.disabled = false;
        const sess = sessions.get(data.id);
        if (sess) {
            sess.status = 'error';
            renderConnStrip();
            if (selectedId === data.id) renderContent();
        }
        addLog('Connection Error: ' + data.error, 'error');
    });

    socket.on('connection_closed', (data) => {
        const sess = sessions.get(data.id);
        if (!sess) return;
        if (sess.userInitiated) {
            // Clean removal — clear from map, switch selection if needed
            sessions.delete(data.id);
            if (selectedId === data.id) {
                selectedId = sessions.size ? sessions.keys().next().value : null;
            }
        } else {
            // Unexpected close — keep visible with 'closed' badge so user knows
            sess.status = 'closed';
            addLog(`Connection dropped: ${sess.name}`, 'error');
        }
        renderConnStrip();
        renderContent();
    });

    // ── tool_result ──────────────────────────────────────────────────────────
    socket.on('tool_result', (data) => {
        addLog(`Tool "${data.tool}" ${data.error ? 'failed' : 'succeeded'}`,
               data.error ? 'error' : 'success');
        if (toolExecModal && !toolExecModal.classList.contains('hidden')) {
            showExecResult(data);
        }
    });

    // ── connection type toggle ───────────────────────────────────────────────
    connType.addEventListener('change', () => {
        const isStdio = connType.value === 'stdio';
        stdioFields.classList.toggle('hidden', !isStdio);
        sseFields.classList.toggle('hidden', isStdio);
    });

    // ── tabs ─────────────────────────────────────────────────────────────────
    document.querySelectorAll('.tab-btn').forEach(btn =>
        btn.addEventListener('click', () => tabApi.setTab(btn.dataset.tab))
    );

    // ── Quick Connect ────────────────────────────────────────────────────────
    connectBtn.addEventListener('click', () => {
        if (isConnecting) return;
        const type = connType.value;
        let target, name;
        if (type === 'stdio') {
            target = document.getElementById('connCommand').value.trim();
            name   = target.split(' ')[0].split('/').pop() || target;
        } else {
            target = document.getElementById('connUrl').value.trim();
            try { name = new URL(target).hostname; } catch { name = target; }
        }
        if (!target) { addLog('Please enter a target.', 'error'); return; }

        isConnecting = true;
        connectBtn.disabled = true;

        const args = (type === 'stdio')
            ? (document.getElementById('connArgs').value.trim().split(/\s+/).filter(Boolean))
            : [];

        startSession(type, target, name, null, args);
    });

    // ── "+" button in connection strip ───────────────────────────────────────
    newConnBtn.addEventListener('click', () => {
        // Scroll sidebar into view and focus the connect button area
        document.getElementById('connCommand').focus();
    });

    clearLogs.addEventListener('click', () => { logs.innerHTML = ''; });

    // ── Core session management ──────────────────────────────────────────────

    /**
     * Create a new session entry and emit 'inspect_server' to the backend.
     * Optionally pass registeredServerId when connecting from the server list.
     */
    function startSession(type, target, name, registeredServerId = null, args = []) {
        const id = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
        sessions.set(id, {
            id, target, type,
            name: name || shorten(target),
            status: 'connecting',
            data: null,
            registeredServerId,
            bannerDismissed: !!registeredServerId,
            userInitiated: false,
            args,
        });
        socket.emit('inspect_server', { id, type, target, args });
        renderConnStrip();
        if (!selectedId) selectSession(id);
        return id;
    }

    /** Switch the visible session. */
    function selectSession(id) {
        selectedId = id;
        renderConnStrip();
        renderContent();
        updateRegisterBanner();
    }

    /** User explicitly disconnects a session. */
    function disconnectSession(id) {
        const sess = sessions.get(id);
        if (!sess) return;
        if (sess.status === 'closed' || sess.status === 'error') {
            // Already terminated — just remove from map
            sessions.delete(id);
            if (selectedId === id) {
                selectedId = sessions.size ? sessions.keys().next().value : null;
            }
            renderConnStrip();
            renderContent();
            return;
        }
        sess.userInitiated = true;
        sess.status = 'disconnecting';
        socket.emit('disconnect_server', { id });
        renderConnStrip();
    }

    /** Reconnect a closed session using its original parameters. */
    function reconnectSession(id) {
        const old = sessions.get(id);
        if (!old) return;
        sessions.delete(id);
        const newId = startSession(old.type, old.target, old.name, old.registeredServerId, old.args || []);
        if (selectedId === id) selectSession(newId);
    }

    // ── Connection strip rendering ────────────────────────────────────────────

    function renderConnStrip() {
        if (sessions.size === 0) {
            connStrip.classList.add('hidden');
            return;
        }
        connStrip.classList.remove('hidden');
        connStripItems.innerHTML = '';

        const dotClass = {
            connecting   : 'bg-yellow-400 animate-pulse',
            active       : 'bg-green-400',
            error        : 'bg-red-500',
            closed       : 'bg-gray-400',
            disconnecting: 'bg-orange-400 animate-pulse',
        };

        for (const sess of sessions.values()) {
            const isSelected = sess.id === selectedId;
            const chip = document.createElement('div');
            chip.className = [
                'flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium border cursor-pointer',
                'transition-all whitespace-nowrap shrink-0 select-none',
                isSelected
                    ? 'bg-indigo-600 text-white border-indigo-600 shadow-sm'
                    : 'bg-white text-gray-600 border-gray-200 hover:border-indigo-300 hover:text-indigo-600',
            ].join(' ');

            const dot = document.createElement('span');
            dot.className = `w-1.5 h-1.5 rounded-full shrink-0 ${dotClass[sess.status] || 'bg-gray-400'}`;

            const label = document.createElement('span');
            label.className = 'max-w-[130px] truncate';
            label.textContent = sess.name;
            label.title = `${sess.target}\n[${sess.id}]`;

            // Status suffix for non-active states
            if (sess.status !== 'active' && sess.status !== 'connecting') {
                const status = document.createElement('span');
                status.className = isSelected ? 'opacity-70 text-[10px]' : 'text-gray-400 text-[10px]';
                status.textContent = sess.status === 'disconnecting' ? '…' : sess.status;
                chip.appendChild(dot); chip.appendChild(label); chip.appendChild(status);
            } else {
                chip.appendChild(dot); chip.appendChild(label);
            }

            // × / remove button
            const xBtn = document.createElement('button');
            xBtn.textContent = '×';
            xBtn.title = (sess.status === 'closed' || sess.status === 'error') ? 'Remove' : 'Disconnect';
            xBtn.className = [
                'ml-0.5 leading-none font-bold text-sm shrink-0 w-4 h-4 flex items-center justify-center rounded-full',
                'transition-colors',
                isSelected
                    ? 'text-white/70 hover:text-white hover:bg-white/20'
                    : 'text-gray-400 hover:text-red-500 hover:bg-red-50',
            ].join(' ');
            xBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                disconnectSession(sess.id);
            });
            chip.appendChild(xBtn);

            chip.addEventListener('click', () => selectSession(sess.id));
            connStripItems.appendChild(chip);
        }
    }

    // ── Content area rendering ────────────────────────────────────────────────

    function renderContent() {
        if (!selectedId || !sessions.has(selectedId)) {
            contentArea.innerHTML = '';
            placeholderState.classList.remove('hidden');
            contentArea.appendChild(placeholderState);
            return;
        }

        placeholderState.classList.add('hidden');
        const sess = sessions.get(selectedId);

        if (sess.status === 'connecting' || sess.status === 'disconnecting') {
            contentArea.innerHTML = `
                <div class="flex flex-col items-center justify-center h-full text-gray-400 gap-4">
                    <i class="fa-solid fa-circle-notch fa-spin text-4xl text-indigo-400"></i>
                    <p class="text-sm">${sess.status === 'connecting' ? 'Connecting to ' + esc(sess.name) + '…' : 'Disconnecting…'}</p>
                </div>`;
            return;
        }

        if (sess.status === 'closed' || sess.status === 'error') {
            contentArea.innerHTML = `
                <div class="flex flex-col items-center justify-center h-full text-gray-400 gap-4">
                    <i class="fa-solid fa-circle-xmark text-4xl ${sess.status === 'error' ? 'text-red-400' : 'text-gray-300'}"></i>
                    <p class="text-sm font-medium text-gray-500">
                        ${sess.status === 'error' ? 'Connection failed' : 'Connection closed'}
                    </p>
                    <p class="text-xs text-gray-400">${esc(sess.name)} · ${esc(sess.target)}</p>
                    <button onclick="window._reconnect('${sess.id}')"
                        class="mt-2 px-4 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 shadow">
                        <i class="fa-solid fa-rotate-right mr-2"></i> Reconnect
                    </button>
                </div>`;
            return;
        }

        // Active — render tools/resources/prompts
        if (!sess.data) { contentArea.innerHTML = ''; return; }
        contentArea.innerHTML = '';
        const activeTab = tabApi.active;
        if (activeTab === 'tools')          renderTools(sess.data.tools, sess);
        else if (activeTab === 'resources') renderResources(sess.data.resources);
        else if (activeTab === 'prompts')   renderPrompts(sess.data.prompts);
    }

    // expose for inline onclick
    window._reconnect = (id) => reconnectSession(id);

    // ── Register banner ───────────────────────────────────────────────────────

    function updateRegisterBanner() {
        if (!selectedId) { registerBanner.classList.add('hidden'); return; }
        const sess = sessions.get(selectedId);
        if (!sess || sess.status !== 'active' || sess.bannerDismissed) {
            registerBanner.classList.add('hidden');
            return;
        }
        // Check if this URL is already in the registered list
        const alreadyReg = registeredServers.some(s => s.url === sess.target);
        if (alreadyReg) {
            sess.registeredServerId = registeredServers.find(s => s.url === sess.target)?.id || null;
            sess.bannerDismissed = true;
            registerBanner.classList.add('hidden');
            return;
        }
        registerBanner.classList.remove('hidden');
    }

    registerBannerNo.addEventListener('click', () => {
        const sess = selectedId ? sessions.get(selectedId) : null;
        if (sess) sess.bannerDismissed = true;
        registerBanner.classList.add('hidden');
    });

    registerBannerYes.addEventListener('click', () => {
        if (!selectedId) return;
        const sess = sessions.get(selectedId);
        if (!sess) return;
        // Pre-fill the add-server modal with this session's details
        _bannerRegisterSessionId = selectedId;
        document.getElementById('newServerName').value = sess.name !== shorten(sess.target) ? sess.name : '';
        document.getElementById('newServerUrl').value  = sess.target;
        document.getElementById('newServerType').value = sess.type === 'stdio' ? 'stdio' : 'http';
        addServerModal.classList.remove('hidden');
    });

    // ── Add server modal ──────────────────────────────────────────────────────
    addServerBtn.addEventListener('click', () => {
        _bannerRegisterSessionId = null;
        addServerModal.classList.remove('hidden');
    });
    cancelAddServer.addEventListener('click', () => {
        _bannerRegisterSessionId = null;
        addServerModal.classList.add('hidden');
    });
    addServerModal.addEventListener('click', (e) => {
        if (e.target === addServerModal) {
            _bannerRegisterSessionId = null;
            addServerModal.classList.add('hidden');
        }
    });

    // Toggle URL vs stdio fields based on the selected type
    document.getElementById('newServerType').addEventListener('change', () => {
        const type = document.getElementById('newServerType').value;
        document.getElementById('newServerUrlWrap').classList.toggle('hidden', type === 'stdio');
        document.getElementById('newServerStdioWrap').classList.toggle('hidden', type !== 'stdio');
    });

    confirmAddServer.addEventListener('click', async () => {
        const name = document.getElementById('newServerName').value.trim();
        const url  = document.getElementById('newServerUrl').value.trim();
        const type = document.getElementById('newServerType').value;
        const command = document.getElementById('newServerCommand').value.trim();
        const argsRaw = document.getElementById('newServerArgs').value.trim();
        const args = argsRaw ? argsRaw.split(',').map(a => a.trim()).filter(Boolean) : [];

        if (!name) return alert('Name is required.');
        if (type === 'stdio') {
            if (!command) return alert('Command is required for stdio servers.');
        } else if (!url) {
            return alert('URL is required.');
        }

        // Check for duplicate URL already in registry (normalised comparison)
        const norm = (u) => u.trim().toLowerCase().replace(/\/+$/, '');
        const existing = type !== 'stdio'
            ? registeredServers.find(s => norm(s.url) === norm(url))
            : null;
        if (existing) {
            // Already registered — just link the session if applicable
            if (_bannerRegisterSessionId) {
                const sess = sessions.get(_bannerRegisterSessionId);
                if (sess) {
                    sess.registeredServerId = existing.id;
                    sess.name = existing.name;
                    sess.bannerDismissed = true;
                }
                renderConnStrip();
                renderContent();
                registerBanner.classList.add('hidden');
                addLog(`Server already registered as "${existing.name}". Session linked.`, 'success');
            } else {
                addLog(`A server with this URL is already registered as "${existing.name}".`, 'error');
            }
            _bannerRegisterSessionId = null;
            addServerModal.classList.add('hidden');
            return;
        }

        const payload = { name, type };
        if (type === 'stdio') {
            payload.command = command;
            payload.args = args;
        } else {
            payload.url = url;
        }

        try {
            const res = await fetch('/api/servers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!res.ok) { const e = await res.json(); alert(e.error); return; }
            const rec = await res.json();
            addLog(`Server "${name}" registered.`, 'success');

            // Link the session (if this came from the register banner)
            if (_bannerRegisterSessionId) {
                const sess = sessions.get(_bannerRegisterSessionId);
                if (sess) {
                    sess.registeredServerId = rec.id;
                    sess.name = name;
                    sess.bannerDismissed = true;
                    renderConnStrip();
                    renderContent();
                }
                registerBanner.classList.add('hidden');
            }

            _bannerRegisterSessionId = null;
            addServerModal.classList.add('hidden');
            document.getElementById('newServerName').value = '';
            document.getElementById('newServerUrl').value  = '';
            document.getElementById('newServerCommand').value = '';
            document.getElementById('newServerArgs').value = '';
            document.getElementById('newServerType').value = 'http';
            document.getElementById('newServerUrlWrap').classList.remove('hidden');
            document.getElementById('newServerStdioWrap').classList.add('hidden');
            loadServers();
        } catch (err) {
            addLog('Failed to register server: ' + err, 'error');
        }
    });

    // ── Server list ───────────────────────────────────────────────────────────
    async function loadServers() {
        try {
            const res = await fetch('/api/servers');
            registeredServers = await res.json();
            renderServerList(registeredServers);
            // Refresh banner state for the selected session
            updateRegisterBanner();
        } catch (err) {
            console.error('Failed to load servers', err);
        }
    }

    function renderServerList(servers) {
        if (!servers.length) {
            serverList.innerHTML = '<div class="text-xs text-gray-400 italic">No servers registered yet.</div>';
            return;
        }
        serverList.innerHTML = '';
        servers.forEach(srv => {
            // Count active sessions pointing at this server
            const activeSessions = [...sessions.values()].filter(
                s => s.registeredServerId === srv.id && s.status === 'active'
            ).length;
            const sessionsBadge = activeSessions
                ? `<span class="px-1.5 py-0.5 bg-green-100 text-green-700 rounded-full text-[9px] font-bold">${activeSessions}</span>`
                : '';

            const el = document.createElement('div');
            el.className = 'flex items-center justify-between bg-gray-50 rounded-lg px-3 py-2 text-xs border border-gray-200 group';
            el.innerHTML = `
                <div class="flex items-center gap-2 min-w-0">
                    <div class="w-2 h-2 rounded-full flex-shrink-0 ${activeSessions > 0 ? 'bg-green-500 pulse-dot' : srv.online ? 'bg-green-500' : 'bg-red-400 pulse-dot'}"></div>
                    <div class="truncate min-w-0">
                        <div class="flex items-center gap-1">
                            <span class="font-semibold text-gray-700 truncate">${esc(srv.name)}</span>
                            ${sessionsBadge}
                        </div>
                        <span class="block text-[10px] text-gray-400 truncate">${esc(srv.conn_type === 'stdio' ? (srv.command || 'stdio') : srv.url)}</span>
                    </div>
                </div>
                <div class="flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity flex-shrink-0">
                    <button title="Refresh" onclick="window._refreshSrv('${srv.id}')" class="text-indigo-500 hover:text-indigo-700 p-1"><i class="fa-solid fa-arrows-rotate text-[10px]"></i></button>
                    <button title="Connect" onclick="window._inspectSrv('${srv.id}',${JSON.stringify(srv.url || '')},'${srv.conn_type}',${JSON.stringify(srv.name || '')},${JSON.stringify(srv.command || '')},${JSON.stringify(srv.args || [])})" class="text-green-500 hover:text-green-700 p-1"><i class="fa-solid fa-plug text-[10px]"></i></button>
                    <button title="Remove" onclick="window._removeSrv('${srv.id}')" class="text-red-400 hover:text-red-600 p-1"><i class="fa-solid fa-trash text-[10px]"></i></button>
                </div>
            `;
            serverList.appendChild(el);
        });
    }

    window._refreshSrv = async (id) => {
        await fetch(`/api/servers/${id}/refresh`, { method: 'POST' });
        addLog('Refresh triggered.', 'info');
        setTimeout(loadServers, 3000);
    };

    window._inspectSrv = (id, url, type, name, command, args) => {
        // Check if we already have an active session for this exact server
        const exists = [...sessions.values()].find(
            s => s.registeredServerId === id && (s.status === 'active' || s.status === 'connecting')
        );
        if (exists) {
            // Just switch to it rather than opening a duplicate
            selectSession(exists.id);
            addLog(`Already connected to "${name}" — switched to existing session.`, 'info');
            return;
        }
        const target = type === 'stdio' ? (command || url) : url;
        startSession(type || 'http', target, name || url, id, args || []);
        addLog(`Connecting to registered server "${name}"…`);
    };

    window._removeSrv = async (id) => {
        if (!confirm('Remove this server?')) return;
        await fetch(`/api/servers/${id}`, { method: 'DELETE' });
        // Unlink any sessions that pointed at this server
        for (const sess of sessions.values()) {
            if (sess.registeredServerId === id) sess.registeredServerId = null;
        }
        loadServers();
    };

    refreshAllBtn.addEventListener('click', async () => {
        await fetch('/api/servers/refresh', { method: 'POST' });
        addLog('Refreshing all servers…', 'info');
        setTimeout(loadServers, 4000);
    });

    // ── Tool search ───────────────────────────────────────────────────────────
    async function doSearch() {
        const q = toolSearchInput.value.trim();
        if (!q) return;
        try {
            const res = await fetch(`/api/tools/search?q=${encodeURIComponent(q)}`);
            const data = await res.json();
            renderSearchResults(data.tools || []);
        } catch (err) {
            addLog('Search failed: ' + err, 'error');
        }
    }

    toolSearchBtn.addEventListener('click', doSearch);
    toolSearchInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

    function renderSearchResults(tools) {
        searchResults.classList.remove('hidden');
        if (!tools.length) {
            searchResults.innerHTML = '<div class="text-xs text-gray-400 italic p-2">No matching tools found.</div>';
            return;
        }
        searchResults.innerHTML = '';
        tools.forEach(tool => {
            const el = document.createElement('div');
            el.className = 'bg-white border border-gray-200 rounded-lg p-3 shadow-sm cursor-pointer hover:border-indigo-300 transition-colors';
            el.title = 'Click to execute';
            const score  = tool._relevance_score !== undefined
                ? `<span class="text-[10px] text-indigo-500 ml-1">${(tool._relevance_score * 100).toFixed(0)}%</span>` : '';
            const server = tool._server_name
                ? `<span class="text-[10px] text-gray-400 block">${esc(tool._server_name)}</span>` : '';
            const desc   = tool.description ? tool.description.substring(0, 120) : 'No description';
            el.innerHTML = `
                <div class="flex items-center gap-1">
                    <span class="font-semibold text-xs text-gray-800">${esc(tool.name)}</span>
                    ${score}
                </div>
                ${server}
                <p class="text-[11px] text-gray-500 mt-1 line-clamp-2">${esc(desc)}</p>`;
            el.addEventListener('click', () => openExecModal(tool));
            searchResults.appendChild(el);
        });
    }

    // ── Tools / Resources / Prompts renderers ─────────────────────────────────

    function renderTools(tools, sess) {
        if (!tools || !tools.length) {
            contentArea.innerHTML = '<div class="text-gray-500 p-4">No tools found.</div>';
            return;
        }
        const grid = document.createElement('div');
        grid.className = 'grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-6';

        tools.forEach(tool => {
            const schema   = tool.inputSchema || {};
            const props    = schema.properties ? Object.keys(schema.properties) : [];
            const required = schema.required || [];
            const paramBadge = props.length
                ? `<span class="text-[10px] text-gray-400 mt-1">${props.length} param${props.length !== 1 ? 's' : ''}${required.length ? ` (${required.length} req.)` : ''}</span>`
                : '';

            const card = document.createElement('div');
            card.className = 'bg-white rounded-xl shadow-sm border border-gray-100 hover:shadow-md transition-shadow p-5 flex flex-col';
            card.innerHTML = `
                <div class="flex items-center justify-between mb-3">
                    <h3 class="font-bold text-gray-800 text-base leading-tight truncate mr-2">${esc(tool.name)}</h3>
                    <span class="px-2 py-1 bg-indigo-50 text-indigo-700 rounded text-xs font-semibold shrink-0">Tool</span>
                </div>
                <p class="text-sm text-gray-600 mb-2 flex-1 line-clamp-3">${esc(tool.description || 'No description available')}</p>
                ${paramBadge}
                <div class="mt-auto pt-3 border-t border-gray-50">
                    <button class="exec-btn w-full py-2 px-4 bg-gray-50 hover:bg-indigo-50 text-gray-700 hover:text-indigo-700 rounded-lg text-sm font-medium transition-colors border border-gray-100 hover:border-indigo-200">
                        <i class="fa-solid fa-play mr-2 text-indigo-500 text-xs"></i> Execute
                    </button>
                </div>`;
            card.querySelector('.exec-btn').addEventListener('click', () => openExecModal(tool, sess));
            grid.appendChild(card);
        });
        contentArea.appendChild(grid);
    }

    function renderResources(resources) {
        if (!resources || !resources.length) {
            contentArea.innerHTML = '<div class="text-gray-500 p-4">No resources found.</div>';
            return;
        }
        const list = document.createElement('div');
        list.className = 'space-y-3';
        resources.forEach(res => {
            const item = document.createElement('div');
            item.className = 'bg-white p-4 rounded-lg border border-gray-200 shadow-sm';
            item.innerHTML = `<h4 class="font-bold text-gray-800">${esc(res.name)}</h4><p class="text-sm text-gray-500 mt-1 font-mono">${esc(res.uri)}</p>`;
            list.appendChild(item);
        });
        contentArea.appendChild(list);
    }

    function renderPrompts(prompts) {
        if (!prompts || !prompts.length) {
            contentArea.innerHTML = '<div class="text-gray-500 p-4">No prompts found.</div>';
            return;
        }
        const list = document.createElement('div');
        list.className = 'space-y-3';
        prompts.forEach(p => {
            const item = document.createElement('div');
            item.className = 'bg-white p-4 rounded-lg border border-gray-200 shadow-sm';
            item.innerHTML = `<h4 class="font-bold text-gray-800">${esc(p.name)}</h4><p class="text-sm text-gray-500 mt-1">${esc(p.description || '')}</p>`;
            list.appendChild(item);
        });
        contentArea.appendChild(list);
    }

    // ── Tool Execute Modal ────────────────────────────────────────────────────

    function openExecModal(tool, sess) {
        // Resolve session to use: prefer the passed-in sess, else the selected session
        const resolvedSess = sess || (selectedId ? sessions.get(selectedId) : null) || null;
        _execTool = { tool, sess: resolvedSess };

        execToolName.textContent = tool.name;
        // Show full description as tooltip; clamp visible text to 2 lines in the header
        const fullDesc = tool.description || '';
        execToolDesc.textContent = fullDesc;
        execToolDesc.title = fullDesc;  // native tooltip on hover

        if (resolvedSess) {
            const abbr = resolvedSess.id.slice(0, 12) + '…';
            execConnBadge.textContent = resolvedSess.name + ' [' + abbr + ']';
            execRestId.textContent = resolvedSess.id;
        } else {
            execConnBadge.textContent = '(no session)';
            execRestId.textContent    = '<connection_id>';
        }

        // Build schema-driven form
        execToolForm.innerHTML = '';
        const schema   = tool.inputSchema || {};
        const props    = schema.properties || {};
        const required = new Set(schema.required || []);

        if (Object.keys(props).length === 0) {
            execToolForm.innerHTML = '<p class="text-xs text-gray-400 italic">No input schema defined — use the JSON field below.</p>';
        }

        Object.entries(props).forEach(([key, def]) => {
            const isReq = required.has(key);
            const wrapper = document.createElement('div');

            const labelEl = document.createElement('label');
            labelEl.className = 'block text-xs font-medium text-gray-700 mb-1';
            labelEl.innerHTML = `<span class="font-semibold">${esc(key)}</span>${isReq ? ' <span class="text-red-500">*</span>' : ''}${def.description ? `<span class="text-[10px] text-gray-400 font-normal ml-1">${esc(def.description)}</span>` : ''}`;

            let input;
            if (def.type === 'boolean') {
                input = document.createElement('select');
                input.className = 'w-full bg-gray-50 border border-gray-300 text-sm rounded-lg p-2';
                input.innerHTML = '<option value="">— choose —</option><option value="true">true</option><option value="false">false</option>';
            } else if (def.enum) {
                input = document.createElement('select');
                input.className = 'w-full bg-gray-50 border border-gray-300 text-sm rounded-lg p-2';
                input.innerHTML = '<option value="">— choose —</option>' +
                    def.enum.map(v => `<option value="${esc(String(v))}">${esc(String(v))}</option>`).join('');
            } else if (def.type === 'integer' || def.type === 'number') {
                input = document.createElement('input');
                input.type = 'number';
                input.className = 'w-full bg-gray-50 border border-gray-300 text-sm rounded-lg p-2';
                if (def.default !== undefined) input.placeholder = String(def.default);
            } else {
                input = document.createElement('input');
                input.type = 'text';
                input.className = 'w-full bg-gray-50 border border-gray-300 text-sm rounded-lg p-2';
                if (def.default !== undefined) input.placeholder = String(def.default);
            }
            input.dataset.argKey  = key;
            input.dataset.argType = def.type || 'string';
            input.addEventListener('input', syncFormToJson);

            wrapper.appendChild(labelEl);
            wrapper.appendChild(input);
            execToolForm.appendChild(wrapper);
        });

        execToolJson.value = '{}';
        execResultArea.classList.add('hidden');
        execResultContent.textContent = '';
        runToolBtn.disabled = false;
        runToolBtn.innerHTML = '<i class="fa-solid fa-play mr-2 text-xs"></i> Execute';

        toolExecModal.classList.remove('hidden');
    }

    function syncFormToJson() {
        execToolJson.value = JSON.stringify(collectFormArgs(), null, 2);
    }

    function collectFormArgs() {
        const args = {};
        execToolForm.querySelectorAll('[data-arg-key]').forEach(input => {
            const key  = input.dataset.argKey;
            const type = input.dataset.argType;
            let val = input.value;
            if (val === '' || val === undefined) return;
            if (type === 'integer') val = parseInt(val, 10);
            else if (type === 'number') val = parseFloat(val);
            else if (type === 'boolean') val = (val === 'true');
            args[key] = val;
        });
        return args;
    }

    function _closeExecModal() {
        toolExecModal.classList.add('hidden');
        _execTool = null;
    }
    closeExecModal.addEventListener('click', _closeExecModal);
    cancelExecModal.addEventListener('click', _closeExecModal);
    toolExecModal.addEventListener('click', (e) => { if (e.target === toolExecModal) _closeExecModal(); });

    runToolBtn.addEventListener('click', () => {
        if (!_execTool) return;
        const { tool, sess } = _execTool;

        let args = {};
        try {
            const jsonStr = execToolJson.value.trim();
            args = jsonStr ? JSON.parse(jsonStr) : collectFormArgs();
        } catch (err) {
            showExecResult({ tool: tool.name, error: 'Invalid JSON: ' + err.message });
            return;
        }

        if (!sess || (sess.status !== 'active')) {
            showExecResult({ tool: tool.name, error: 'No active session — connect to an MCP server first.' });
            return;
        }

        runToolBtn.disabled = true;
        runToolBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-2 text-xs"></i> Running…';
        execResultArea.classList.add('hidden');
        addLog(`Executing tool "${tool.name}" on "${sess.name}"…`);

        socket.emit('execute_tool', {
            tool_name: tool.name,
            arguments: args,
            connection_id: sess.id,
        });
    });

    function showExecResult(data) {
        execResultArea.classList.remove('hidden');
        runToolBtn.disabled = false;
        runToolBtn.innerHTML = '<i class="fa-solid fa-play mr-2 text-xs"></i> Execute';

        if (data.error) {
            execResultStatus.textContent = 'Error';
            execResultStatus.className = 'px-2 py-0.5 rounded-full text-white text-[10px] font-semibold bg-red-500';
            execResultContent.textContent = data.error;
            execResultContent.className = 'bg-gray-900 text-red-300 rounded-lg p-3 text-xs overflow-auto max-h-52 whitespace-pre-wrap break-words font-mono';
        } else {
            execResultStatus.textContent = 'Success';
            execResultStatus.className = 'px-2 py-0.5 rounded-full text-white text-[10px] font-semibold bg-green-500';
            const payload = data.result;
            let display = '';
            if (payload && payload.content && Array.isArray(payload.content)) {
                display = payload.content
                    .map(c => c.text !== undefined ? c.text : JSON.stringify(c, null, 2))
                    .join('\n---\n');
            } else {
                display = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
            }
            execResultContent.textContent = display;
            execResultContent.className = 'bg-gray-900 text-green-300 rounded-lg p-3 text-xs overflow-auto max-h-52 whitespace-pre-wrap break-words font-mono';
        }
    }

    // ── Utils ─────────────────────────────────────────────────────────────────
    function addLog(msg, type = 'info') {
        const div = document.createElement('div');
        div.className = 'py-1 border-b border-gray-800 font-mono text-xs break-words';
        if (type === 'error')   div.className += ' text-red-400';
        if (type === 'success') div.className += ' text-green-400';
        div.innerText = `[${new Date().toLocaleTimeString()}] ${msg}`;
        logs.appendChild(div);
        logs.scrollTop = logs.scrollHeight;
    }

    function esc(str) {
        if (!str) return '';
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }

    function shorten(str, max = 32) {
        if (!str) return '(unknown)';
        try { return new URL(str).hostname; } catch { /* not a URL */ }
        return str.length > max ? str.slice(0, max) + '…' : str;
    }

    // ── Init ──────────────────────────────────────────────────────────────────
    loadServers();
    setInterval(loadServers, 30000);
});
