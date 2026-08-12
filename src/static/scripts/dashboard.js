// Theme Management
function setTheme(theme) {
    document.body.className = `theme-${theme}`;
    localStorage.setItem('inkypi_theme', theme);
    updateThemeButtons(theme);
}

function updateThemeButtons(theme) {
    document.querySelectorAll('.theme-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.getAttribute('data-theme') === theme) {
            btn.classList.add('active');
        }
    });
}

// Initialize theme from localStorage
function initTheme() {
    const savedTheme = localStorage.getItem('inkypi_theme') || 'light';
    setTheme(savedTheme);
}

// Settings Modal
function openSettings() {
    document.getElementById('settingsModal').classList.add('show');
}

function closeSettings() {
    document.getElementById('settingsModal').classList.remove('show');
}

// Close modal when clicking outside
window.addEventListener('click', (event) => {
    const modal = document.getElementById('settingsModal');
    if (event.target === modal) {
        closeSettings();
    }
});

// Welcome Message
function saveWelcomeName() {
    const name = document.getElementById('welcomeInput').value || "Swaroop's TRMNL";
    fetch('/api/settings/welcome', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            document.querySelector('.welcome-title').textContent = name;
            showStatus('Welcome message updated!', 'success');
        }
    })
    .catch(err => showStatus('Error saving welcome message', 'error'));
}

// WiFi Scanning and Connection
async function scanWifiNetworks() {
    const select = document.getElementById('wifiNetworks');
    select.innerHTML = '<option value="">Scanning...</option>';
    
    try {
        const response = await fetch('/api/wifi/scan');
        const data = await response.json();
        
        select.innerHTML = '<option value="">Select a network...</option>';
        data.networks.forEach(network => {
            const option = document.createElement('option');
            option.value = network.ssid;
            option.textContent = `${network.ssid} (${network.signal}%)`;
            select.appendChild(option);
        });
        
        showWiFiStatus('Networks scanned', 'success');
    } catch (err) {
        showWiFiStatus('Failed to scan networks', 'error');
    }
}

function selectWifiNetwork() {
    const ssid = document.getElementById('wifiNetworks').value;
    if (ssid) {
        // Pre-fill SSID for user
        console.log('Selected network:', ssid);
    }
}

async function connectWifi() {
    const ssid = document.getElementById('wifiNetworks').value;
    const password = document.getElementById('wifiPassword').value;
    
    if (!ssid || !password) {
        showWiFiStatus('Please select a network and enter password', 'error');
        return;
    }
    
    showWiFiStatus('Connecting...', 'info');
    
    try {
        const response = await fetch('/api/wifi/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ssid, password })
        });
        
        const data = await response.json();
        if (data.success) {
            showWiFiStatus('Connected successfully!', 'success');
            document.getElementById('wifiPassword').value = '';
        } else {
            showWiFiStatus(data.error || 'Connection failed', 'error');
        }
    } catch (err) {
        showWiFiStatus('Connection error: ' + err.message, 'error');
    }
}

function showWiFiStatus(message, type) {
    const status = document.getElementById('wifiStatus');
    status.textContent = message;
    status.className = `wifi-status ${type}`;
}

// Plugin Management
function refreshPlugin(event, pluginId) {
    event.stopPropagation();
    const btn = event.target;
    btn.style.animation = 'spin 1s linear infinite';
    
    fetch(`/api/plugin/${pluginId}/refresh`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            btn.style.animation = '';
            if (data.success) {
                showStatus('Plugin refreshed', 'success');
                // Update display image
                setTimeout(() => {
                    const img = document.getElementById('displayImage');
                    img.src = img.src.split('?')[0] + '?t=' + Date.now();
                }, 1000);
            }
        })
        .catch(err => {
            btn.style.animation = '';
            showStatus('Refresh failed', 'error');
        });
}

function openPluginSettings(pluginId) {
    // Navigate to plugin settings page (if exists)
    window.location.href = `/plugin/${pluginId}/settings`;
}

function switchPlaylist() {
    const playlistId = document.getElementById('playlistSelect').value;
    fetch('/api/playlist/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ playlist_id: playlistId })
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            location.reload();
        }
    })
    .catch(err => showStatus('Error switching playlist', 'error'));
}

// Display Management
function refreshDisplay() {
    const btn = event.target;
    btn.disabled = true;
    btn.textContent = 'Refreshing...';
    
    fetch('/api/display/refresh', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                showStatus('Display refreshed', 'success');
                setTimeout(() => {
                    const img = document.getElementById('displayImage');
                    img.src = img.src.split('?')[0] + '?t=' + Date.now();
                }, 500);
            }
        })
        .catch(err => showStatus('Refresh failed', 'error'))
        .finally(() => {
            btn.disabled = false;
            btn.textContent = 'Refresh Display';
        });
}

// Status Notifications
function showStatus(message, type = 'info') {
    const toast = document.createElement('div');
    toast.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        padding: 14px 20px;
        background-color: var(--accent);
        color: white;
        border-radius: var(--radius);
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        z-index: 9999;
        animation: slideUp 0.3s ease-out;
        max-width: 300px;
    `;
    
    if (type === 'success') {
        toast.style.backgroundColor = 'var(--success)';
    } else if (type === 'error') {
        toast.style.backgroundColor = 'var(--error)';
    }
    
    toast.textContent = message;
    document.body.appendChild(toast);
    
    setTimeout(() => {
        toast.style.animation = 'fadeOut 0.3s ease-out forwards';
        setTimeout(() => toast.remove(), 300);
    }, 3000);
}

// Add spin animation
const style = document.createElement('style');
style.textContent = `
    @keyframes spin {
        from { transform: rotate(0deg); }
        to { transform: rotate(360deg); }
    }
    @keyframes fadeOut {
        from { opacity: 1; }
        to { opacity: 0; }
    }
`;
document.head.appendChild(style);

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
    initTheme();
    
    // Load welcome message from settings
    fetch('/api/settings/welcome')
        .then(r => r.json())
        .then(data => {
            document.getElementById('welcomeInput').value = data.name || '';
        })
        .catch(err => console.log('Could not load welcome message'));
});
