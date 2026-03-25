/**
 * really.ai v2 — WhatsApp bridge (whatsapp-web.js)
 *
 * Responsibilities:
 *  - Maintain a persistent WhatsApp Web session via LocalAuth.
 *  - Print a QR code to stdout on first run (before session is established).
 *  - Forward every inbound WhatsApp message to the Python FastAPI backend.
 *  - Expose an internal Express HTTP server so the Python backend can send
 *    messages and initiate WhatsApp calls.
 *
 * Environment variables:
 *  PORT                — Express listen port (default: 3001)
 *  PYTHON_BACKEND_URL  — Base URL of the FastAPI backend (default: http://localhost:8000)
 */

'use strict';

const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const axios = require('axios');

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const PORT = parseInt(process.env.PORT || '3001', 10);
const PYTHON_BACKEND_URL = (process.env.PYTHON_BACKEND_URL || 'http://localhost:8000').replace(/\/$/, '');

// ---------------------------------------------------------------------------
// WhatsApp client setup
// ---------------------------------------------------------------------------

const client = new Client({
    authStrategy: new LocalAuth({
        dataPath: './.wwebjs_auth',
    }),
    puppeteer: {
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-dev-shm-usage',
            '--disable-accelerated-2d-canvas',
            '--no-first-run',
            '--no-zygote',
            '--disable-gpu',
        ],
        headless: true,
    },
});

// ---------------------------------------------------------------------------
// WhatsApp client event handlers
// ---------------------------------------------------------------------------

/**
 * R-WA-02: On startup, if no session exists, emit a QR code to stdout for
 * initial scan.
 */
client.on('qr', (qr) => {
    console.log('[really.ai bridge] Scan the QR code below with your WhatsApp app:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    console.log('[really.ai bridge] WhatsApp client is ready and authenticated.');
});

client.on('authenticated', () => {
    console.log('[really.ai bridge] Session authenticated — credentials saved via LocalAuth.');
});

client.on('auth_failure', (msg) => {
    console.error('[really.ai bridge] Authentication failure:', msg);
    console.error('[really.ai bridge] Delete .wwebjs_auth directory and restart to re-scan QR.');
});

/**
 * R-WA-05: Handle disconnection — whatsapp-web.js will attempt to reconnect
 * automatically; log the reason so operators are aware.
 */
client.on('disconnected', (reason) => {
    console.warn('[really.ai bridge] Client disconnected. Reason:', reason);
    console.warn('[really.ai bridge] Attempting to reinitialise…');
    // Re-initialise the client so it reconnects automatically.
    client.initialize().catch((err) => {
        console.error('[really.ai bridge] Failed to reinitialise after disconnect:', err.message);
    });
});

/**
 * R-WA-04: Forward every inbound message to the Python FastAPI backend.
 *
 * Payload: { from, body, timestamp }
 */
client.on('message', async (msg) => {
    // Skip status broadcast messages from WhatsApp's own status channel.
    if (msg.from === 'status@broadcast') {
        return;
    }

    const payload = {
        from: msg.from,
        body: msg.body,
        timestamp: msg.timestamp,
    };

    try {
        const response = await axios.post(
            `${PYTHON_BACKEND_URL}/api/whatsapp/inbound`,
            payload,
            {
                headers: { 'Content-Type': 'application/json' },
                timeout: 30000,
            }
        );
        console.log(
            `[really.ai bridge] Forwarded message from ${msg.from} → backend responded ${response.status}`
        );
    } catch (err) {
        const status = err.response ? err.response.status : 'no response';
        console.error(
            `[really.ai bridge] Failed to forward message from ${msg.from} to backend (HTTP ${status}):`,
            err.message
        );
    }
});

// Initialise the client (starts Puppeteer + loads session or shows QR).
client.initialize().catch((err) => {
    console.error('[really.ai bridge] Fatal error during client.initialize():', err);
    process.exit(1);
});

// ---------------------------------------------------------------------------
// Express HTTP server — internal API for the Python backend
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());

/**
 * Health check — useful for Docker health checks and monitoring.
 */
app.get('/health', (_req, res) => {
    const state = client.info ? 'ready' : 'initialising';
    res.json({ status: 'ok', whatsapp_state: state });
});

/**
 * R-WA-03: POST /send-message
 *
 * Send a text message to a WhatsApp number.
 *
 * Request body:
 *   { "to": "14155551234@c.us", "message": "Hello!" }
 *
 * Response:
 *   200 { "success": true, "messageId": "..." }
 *   400 { "error": "..." }
 *   503 { "error": "WhatsApp client not ready" }
 */
app.post('/send-message', async (req, res) => {
    const { to, message } = req.body;

    if (!to || !message) {
        return res.status(400).json({ error: '`to` and `message` fields are required.' });
    }

    if (!client.info) {
        return res.status(503).json({ error: 'WhatsApp client is not ready yet. Try again shortly.' });
    }

    try {
        const result = await client.sendMessage(to, message);
        console.log(`[really.ai bridge] Sent message to ${to}. MsgId: ${result.id._serialized}`);
        return res.json({ success: true, messageId: result.id._serialized });
    } catch (err) {
        console.error(`[really.ai bridge] Failed to send message to ${to}:`, err.message);
        return res.status(500).json({ error: err.message });
    }
});

/**
 * R-WA-03: POST /send-call
 *
 * Initiate a WhatsApp voice call to a number.
 *
 * Request body:
 *   { "to": "14155551234@c.us" }
 *
 * Note: whatsapp-web.js does not expose a stable first-class call API at the
 * time of writing (v1.23).  The implementation below uses puppeteer page
 * evaluation to interact with the WhatsApp Web UI's internal call mechanism.
 * This is inherently fragile and may break with WhatsApp Web updates.
 * A TODO is logged and the response includes a warning so operators are aware.
 *
 * If the call succeeds the response is { success: true }.
 * If it fails the response is { success: false, warning: "..." }.
 */
app.post('/send-call', async (req, res) => {
    const { to } = req.body;

    if (!to) {
        return res.status(400).json({ error: '`to` field is required.' });
    }

    if (!client.info) {
        return res.status(503).json({ error: 'WhatsApp client is not ready yet. Try again shortly.' });
    }

    // Attempt to use whatsapp-web.js internal call API via puppeteer.
    try {
        const page = client.pupPage;

        if (!page) {
            throw new Error('Puppeteer page not available — client may not be fully ready.');
        }

        // whatsapp-web.js exposes window.WWebJS and the internal WA store.
        // We use the WA Web internal API to start a voice call.
        // This is the most reliable approach without a stable public API.
        const callResult = await page.evaluate(async (chatId) => {
            try {
                // Access WA Web's internal call module via the global Store.
                const wid = window.Store.WidFactory.createWid(chatId);
                const chat = await window.Store.Chat.find(wid);
                if (!chat) {
                    return { success: false, error: `Chat not found for ${chatId}` };
                }
                // Initiate a voice call (audio only, not video).
                await window.Store.Call.startCall(chat, false /* isVideo */);
                return { success: true };
            } catch (e) {
                return { success: false, error: e.message || String(e) };
            }
        }, to);

        if (callResult.success) {
            console.log(`[really.ai bridge] Voice call initiated to ${to}.`);
            return res.json({ success: true });
        } else {
            // TODO: whatsapp-web.js call API is not stable — monitor upstream
            // https://github.com/wwebjs/whatsapp-web.js for updates.
            console.warn(
                `[really.ai bridge] Call to ${to} failed via internal API: ${callResult.error}. ` +
                'This feature requires whatsapp-web.js to expose a stable call API. ' +
                'Consider using VAPI as a fallback (see R-WA-11).'
            );
            return res.status(200).json({
                success: false,
                warning:
                    'WhatsApp call initiation is not fully supported by whatsapp-web.js. ' +
                    'Use the VAPI fallback for reliable call delivery.',
                detail: callResult.error,
            });
        }
    } catch (err) {
        console.error(`[really.ai bridge] Error while attempting call to ${to}:`, err.message);
        // Return 200 so the Python backend can decide to fall back to VAPI rather
        // than treating this as a fatal HTTP error.
        return res.status(200).json({
            success: false,
            warning: 'WhatsApp call initiation threw an unexpected error. VAPI fallback recommended.',
            detail: err.message,
        });
    }
});

// ---------------------------------------------------------------------------
// Start Express server
// ---------------------------------------------------------------------------

app.listen(PORT, () => {
    console.log(`[really.ai bridge] Express server listening on port ${PORT}`);
    console.log(`[really.ai bridge] Forwarding inbound messages to: ${PYTHON_BACKEND_URL}/api/whatsapp/inbound`);
});

// ---------------------------------------------------------------------------
// Graceful shutdown
// ---------------------------------------------------------------------------

async function shutdown(signal) {
    console.log(`[really.ai bridge] Received ${signal} — shutting down gracefully…`);
    try {
        await client.destroy();
        console.log('[really.ai bridge] WhatsApp client destroyed cleanly.');
    } catch (err) {
        console.error('[really.ai bridge] Error during client.destroy():', err.message);
    }
    process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
