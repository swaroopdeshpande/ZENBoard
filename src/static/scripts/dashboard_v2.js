// ═══════════════════════════════════════════════════════════
// PRESERVED FROM ORIGINAL inky.html - Sort/List toggle
// ═══════════════════════════════════════════════════════════
const viewToggle = document.getElementById('viewToggle');
const pluginsContainer = document.getElementById('pluginsContainer');
let currentView = localStorage.getItem('inkypi-plugins-view') || 'grid';

function updateView() {
    if (currentView === 'list') {
        pluginsContainer.classList.add('list-view');
        viewToggle.textContent = 'Grid';
    } else {
        pluginsContainer.classList.remove('list-view');
        viewToggle.textContent = 'List';
    }
}

viewToggle.addEventListener('click', () => {
    currentView = currentView === 'grid' ? 'list' : 'grid';
    localStorage.setItem('inkypi-plugins-view', currentView);
    updateView();
});

updateView();

const sortToggle = document.getElementById('sortToggle');
const sortHint = document.getElementById('sortHint');
let isSorting = false;
let draggedItem = null;

function enableSorting() {
    isSorting = true;
    sortToggle.classList.add('active');
    sortToggle.textContent = 'Save';
    sortHint.classList.add('visible');
    pluginsContainer.classList.add('sorting');

    document.querySelectorAll('.plugin-item').forEach(item => {
        item.draggable = true;
        item.addEventListener('dragstart', handleDragStart);
        item.addEventListener('dragend', handleDragEnd);
        item.addEventListener('dragover', handleDragOver);
        item.addEventListener('dragleave', handleDragLeave);
        item.addEventListener('drop', handleDrop);
        item.addEventListener('click', preventClick);
    });
}

function disableSorting() {
    isSorting = false;
    sortToggle.classList.remove('active');
    sortToggle.textContent = 'Sort';
    sortHint.classList.remove('visible');
    pluginsContainer.classList.remove('sorting');

    document.querySelectorAll('.plugin-item').forEach(item => {
        item.draggable = false;
        item.removeEventListener('dragstart', handleDragStart);
        item.removeEventListener('dragend', handleDragEnd);
        item.removeEventListener('dragover', handleDragOver);
        item.removeEventListener('dragleave', handleDragLeave);
        item.removeEventListener('drop', handleDrop);
        item.removeEventListener('click', preventClick);
    });

    savePluginOrder();
}

function preventClick(e) {
    if (isSorting) e.preventDefault();
}

function handleDragStart(e) {
    draggedItem = this;
    this.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
}

function handleDragEnd(e) {
    this.classList.remove('dragging');
    document.querySelectorAll('.plugin-item').forEach(item => item.classList.remove('drag-over'));
}

function handleDragOver(e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    if (this !== draggedItem) this.classList.add('drag-over');
}

function handleDragLeave(e) {
    this.classList.remove('drag-over');
}

function handleDrop(e) {
    e.preventDefault();
    this.classList.remove('drag-over');
    if (this !== draggedItem) {
        const items = Array.from(pluginsContainer.children);
        const draggedIndex = items.indexOf(draggedItem);
        const targetIndex = items.indexOf(this);
        if (draggedIndex < targetIndex) {
            this.parentNode.insertBefore(draggedItem, this.nextSibling);
        } else {
            this.parentNode.insertBefore(draggedItem, this);
        }
    }
}

async function savePluginOrder() {
    const order = Array.from(pluginsContainer.children).map(item => item.dataset.pluginId);
    try {
        const response = await fetch('/api/plugin_order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: order })
        });
        if (!response.ok) console.error('Failed to save plugin order');
    } catch (error) {
        console.error('Error saving plugin order:', error);
    }
}

sortToggle.addEventListener('click', () => {
    if (isSorting) disableSorting();
    else enableSorting();
});

// ═══════════════════════════════════════════════════════════
// NEW: Theme Switching
// ═══════════════════════════════════════════════════════════

const THEME_LED_COLORS = {
    light:  { color: '#FFFFFF', mode: 'static',    brightness: 255 },
    dark:   { color: '#0000FF', mode: 'breathe',   brightness: 255 },
    ocean:  { color: '#0077FF', mode: 'static',    brightness: 255 },
    forest: { color: '#00FF44', mode: 'static',    brightness: 255 },
    sunset: { color: '#FF4500', mode: 'static',    brightness: 255 },
    nord:   { color: '#88C0D0', mode: 'static',    brightness: 255 },
};

function setTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('inkypi-theme', theme);
    
    // Sync LEDs to theme
    const led = THEME_LED_COLORS[theme];
    if (led) {
        fetch('/api/led/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ...led, enabled: true })
        });
        // Update LED panel UI if visible
        const modeEl = document.getElementById('ledMode');
        const colorEl = document.getElementById('ledColor');
        const brEl = document.getElementById('ledBrightness');
        if (modeEl) modeEl.value = led.mode;
        if (colorEl) colorEl.value = led.color;
        if (brEl) {
            brEl.value = led.brightness;
            updateBrightnessLabel(led.brightness);
        }
    }
    document.querySelectorAll('.theme-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.theme === theme);
    });
}

document.querySelectorAll('.theme-btn').forEach(btn => {
    btn.addEventListener('click', () => setTheme(btn.dataset.theme));
});

// Highlight current theme on load
const savedTheme = localStorage.getItem('inkypi-theme') || 'light';
setTheme(savedTheme);

// Simple theme cycle button in header (🎨)
const themeBtn = document.getElementById('themeBtn');
const themeOrder = ['light', 'dark', 'ocean', 'forest', 'sunset', 'nord'];
themeBtn.addEventListener('click', () => {
    const current = localStorage.getItem('inkypi-theme') || 'light';
    const nextIndex = (themeOrder.indexOf(current) + 1) % themeOrder.length;
    setTheme(themeOrder[nextIndex]);
});

// ═══════════════════════════════════════════════════════════
// NEW: Dashboard Settings Modal
// ═══════════════════════════════════════════════════════════

const dashSettingsModal = document.getElementById('dashSettingsModal');
const dashSettingsBtn = document.getElementById('dashSettingsBtn');
const closeDashSettings = document.getElementById('closeDashSettings');

dashSettingsBtn.addEventListener('click', () => {
    dashSettingsModal.classList.add('show');
    loadWelcomeName();
});

closeDashSettings.addEventListener('click', () => {
    dashSettingsModal.classList.remove('show');
});

window.addEventListener('click', (e) => {
    if (e.target === dashSettingsModal) {
        dashSettingsModal.classList.remove('show');
    }
});

// ═══════════════════════════════════════════════════════════
// NEW: Welcome Message
// ═══════════════════════════════════════════════════════════

async function loadWelcomeName() {
    try {
        const res = await fetch('/api/settings/welcome');
        const data = await res.json();
        document.getElementById('welcomeInput').value = data.name || '';
    } catch (err) {
        console.error('Could not load welcome name:', err);
    }
}

document.getElementById('saveWelcomeBtn').addEventListener('click', async () => {
    const name = document.getElementById('welcomeInput').value.trim() || "Swaroop's TRMNL";
    try {
        const res = await fetch('/api/settings/welcome', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name })
        });
        const data = await res.json();
        if (data.success) {
            document.getElementById('welcomeTitle').textContent = name;
        }
    } catch (err) {
        console.error('Error saving welcome name:', err);
    }
});

// ═══════════════════════════════════════════════════════════
// NEW: WiFi Scanning + Connection
// ═══════════════════════════════════════════════════════════

document.getElementById('scanWifiBtn').addEventListener('click', async () => {
    const select = document.getElementById('wifiNetworks');
    select.innerHTML = '<option value="">Scanning...</option>';

    try {
        const res = await fetch('/api/wifi/scan');
        const data = await res.json();

        if (data.error) {
            select.innerHTML = `<option value="">${data.error}</option>`;
            return;
        }

        select.innerHTML = '<option value="">Select a network...</option>';
        data.networks.forEach(network => {
            const opt = document.createElement('option');
            opt.value = network.ssid;
            opt.textContent = `${network.ssid} (${network.signal}%)`;
            select.appendChild(opt);
        });
    } catch (err) {
        select.innerHTML = '<option value="">Scan failed</option>';
    }
});

document.getElementById('connectWifiBtn').addEventListener('click', async () => {
    const ssid = document.getElementById('wifiNetworks').value;
    const password = document.getElementById('wifiPassword').value;
    const statusEl = document.getElementById('wifiStatus');

    if (!ssid || !password) {
        statusEl.textContent = 'Select a network and enter password';
        statusEl.className = 'wifi-status error';
        return;
    }

    statusEl.textContent = 'Connecting...';
    statusEl.className = 'wifi-status info';

    try {
        const res = await fetch('/api/wifi/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password })
        });
        const data = await res.json();

        if (data.success) {
            statusEl.textContent = data.message || 'Connected!';
            statusEl.className = 'wifi-status success';
            document.getElementById('wifiPassword').value = '';
        } else {
            statusEl.textContent = data.error || 'Connection failed';
            statusEl.className = 'wifi-status error';
        }
    } catch (err) {
        statusEl.textContent = 'Connection error';
        statusEl.className = 'wifi-status error';
    }
});
