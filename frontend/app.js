/**
 * Cascade — AI Tutor Frontend
 * Handles microphone input, WebSocket communication, audio playback, and latency measurement
 */

// Configuration
const CONFIG = {
    WS_HOST: window.location.hostname || 'localhost',
    WS_PORT: window.location.port || '8000',
    AUDIO_SAMPLE_RATE: 16000,
    SILENCE_THRESHOLD: 0.02,
    SILENCE_DURATION_MS: 800,
};

// Application state
const STATE = {
    IDLE: 'IDLE',
    LISTENING: 'LISTENING',
    PROCESSING: 'PROCESSING',
    SPEAKING: 'SPEAKING',
};

class CascadeClient {
    constructor() {
        this.state = STATE.IDLE;
        this.ws = null;
        this.audioContext = null;
        this.processor = null;
        this.mediaStream = null;
        this.isRecording = false;

        // Audio playback state
        this.audioPlaybackQueue = [];
        this.isPlaying = false;
        this.audioBuffer = null;

        // Latency tracking
        this.utteranceStartTime = null;
        this.firstAudioTime = null;
        this.lastUtteredTime = null;
        this.silenceStartTime = null;

        // UI references
        this.startBtn = document.getElementById('start-btn');
        this.statusBadge = document.getElementById('status-badge');
        this.statusDot = document.getElementById('status-dot');
        this.statusText = document.getElementById('status-text');
        this.latencyValue = document.getElementById('latency-value');
        this.transcriptList = document.getElementById('transcript-list');
        this.subjectSelect = document.getElementById('subject-select');
        this.debugText = document.getElementById('debug-text');

        // Bind event listeners
        this.startBtn.addEventListener('click', () => this.toggleSession());

        this.init();
    }

    async init() {
        console.log('Cascade Client initializing...');
        try {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
            console.log('✓ AudioContext created');
        } catch (err) {
            console.error('Failed to create AudioContext:', err);
            this.showError('Browser audio support not available');
        }
    }

    /**
     * Toggle session start/stop
     */
    async toggleSession() {
        if (this.state === STATE.IDLE) {
            await this.startSession();
        } else {
            await this.stopSession();
        }
    }

    /**
     * Start a new tutoring session
     */
    async startSession() {
        try {
            // Request microphone permission
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: false,
                    sampleRate: { ideal: 16000 },
                },
            });
            console.log('✓ Microphone permission granted');

            // Initialize Web Audio processing
            await this.initAudioProcessing();

            // Connect to WebSocket
            await this.connectWebSocket();

            // Update UI
            this.setState(STATE.LISTENING);
            this.startBtn.textContent = 'Stop Session';
            this.subjectSelect.disabled = true;
            this.transcriptList.innerHTML = '';
            this.addTranscriptItem('welcome', 'Connected. Ask me a question.');
        } catch (err) {
            console.error('Failed to start session:', err);
            this.showError('Failed to start session: ' + err.message);
            await this.stopSession();
        }
    }

    /**
     * Initialize Web Audio API for mic capture
     */
    async initAudioProcessing() {
        if (!this.mediaStream || !this.audioContext) {
            throw new Error('MediaStream or AudioContext not available');
        }

        const source = this.audioContext.createMediaStreamSource(this.mediaStream);
        const workletUrl = this.getAudioWorkletCode();

        // Try to use AudioWorklet (modern approach)
        try {
            const blob = new Blob([workletUrl], { type: 'application/javascript' });
            const url = URL.createObjectURL(blob);
            await this.audioContext.audioWorklet.addModule(url);
            this.processor = new AudioWorkletNode(this.audioContext, 'audio-processor');
            this.processor.port.onmessage = (evt) => this.onAudioProcess(evt.data);
            source.connect(this.processor);
            this.processor.connect(this.audioContext.destination);
            console.log('✓ AudioWorklet processor created');
        } catch (err) {
            console.warn('AudioWorklet not supported, falling back to ScriptProcessor');
            // Fallback to ScriptProcessor
            this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
            this.processor.onaudioprocess = (evt) =>
                this.onAudioProcess(this.pcmEncode(evt.inputBuffer.getChannelData(0)));
            source.connect(this.processor);
            this.processor.connect(this.audioContext.destination);
        }

        this.isRecording = true;
    }

    /**
     * Get AudioWorklet processor code as inline string
     */
    getAudioWorkletCode() {
        return `
            class AudioProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.leftchannel = [];
                    this.rightchannel = [];
                    this.recordingLength = 0;
                }

                process(inputs, outputs) {
                    const left = inputs[0][0];
                    this.leftchannel.push(new Float32Array(left));
                    this.recordingLength += left.length;

                    if (this.recordingLength > 16000) {
                        const pcm = this.downsampleBuffer(left, this.recordingLength);
                        this.port.postMessage({ pcm });
                        this.leftchannel = [];
                        this.recordingLength = 0;
                    }
                    return true;
                }

                downsampleBuffer(buffer, len) {
                    if (buffer.length === 0) return new Uint8Array();
                    const compressed = new Float32Array(len);
                    for (let i = 0; i < len; i++) {
                        compressed[i] = buffer[i];
                    }
                    return this.pcmEncode(compressed);
                }

                pcmEncode(samples) {
                    const length = samples.length * 2 + 44;
                    const arrayBuffer = new ArrayBuffer(length);
                    const view = new DataView(arrayBuffer);
                    const channels = [samples];
                    let offset = 0;
                    let pos = 0;

                    const setUint16 = (data) => {
                        view.setUint16(pos, data, true);
                        pos += 2;
                    };
                    const setUint32 = (data) => {
                        view.setUint32(pos, data, true);
                        pos += 4;
                    };

                    setUint32(0x46464952);
                    setUint32(length - 8);
                    setUint32(0x45564157);
                    setUint32(0x20746d66);
                    setUint32(16);
                    setUint16(1);
                    setUint16(channels.length);
                    setUint32(16000);
                    setUint32(16000 * 2);
                    setUint16(channels.length * 2);
                    setUint16(16);
                    setUint32(0x61746164);
                    setUint32(length - pos - 4);

                    const volume = 0.8;
                    for (let i = 0; i < samples.length; i++, offset += 2) {
                        const s = Math.max(-1, Math.min(1, samples[i]));
                        view.setInt16(pos + offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
                    }

                    return new Uint8Array(arrayBuffer);
                }
            }

            registerProcessor('audio-processor', AudioProcessor);
        `;
    }

    /**
     * PCM encoding helper (for ScriptProcessor fallback)
     */
    pcmEncode(samples) {
        let offset = 0;
        const length = samples.length * 2 + 44;
        const arrayBuffer = new ArrayBuffer(length);
        const view = new DataView(arrayBuffer);
        const channels = [samples];

        const setUint16 = (data) => {
            view.setUint16(offset, data, true);
            offset += 2;
        };
        const setUint32 = (data) => {
            view.setUint32(offset, data, true);
            offset += 4;
        };

        setUint32(0x46464952);
        setUint32(length - 8);
        setUint32(0x45564157);
        setUint32(0x20746d66);
        setUint32(16);
        setUint16(1);
        setUint16(channels.length);
        setUint32(16000);
        setUint32(16000 * 2);
        setUint16(channels.length * 2);
        setUint16(16);
        setUint32(0x61746164);
        setUint32(length - offset - 4);

        const volume = 0.8;
        let audioOffset = offset;
        for (let i = 0; i < samples.length; i++, audioOffset += 2) {
            const s = Math.max(-1, Math.min(1, samples[i]));
            view.setInt16(offset + audioOffset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        }

        return new Uint8Array(arrayBuffer);
    }

    /**
     * Process audio chunks from microphone
     */
    onAudioProcess(pcmData) {
        if (!this.isRecording || this.state === STATE.IDLE) return;

        // Send audio to server
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(pcmData);
        }

        // Detect silence for end-of-utterance
        this.detectSilence(pcmData);
    }

    /**
     * Detect silence to determine end-of-utterance
     */
    detectSilence(pcmData) {
        // Simple RMS-based silence detection
        let sum = 0;
        const view = new DataView(pcmData);
        for (let i = 0; i < view.byteLength; i += 2) {
            const sample = view.getInt16(i, true) / 32768;
            sum += sample * sample;
        }
        const rms = Math.sqrt(sum / (view.byteLength / 2));

        if (rms < CONFIG.SILENCE_THRESHOLD) {
            if (!this.silenceStartTime) {
                this.silenceStartTime = Date.now();
            } else if (Date.now() - this.silenceStartTime > CONFIG.SILENCE_DURATION_MS) {
                if (this.state === STATE.LISTENING) {
                    this.setState(STATE.PROCESSING);
                    this.utteranceStartTime = Date.now();
                    this.silenceStartTime = null;
                }
            }
        } else {
            this.silenceStartTime = null;
            this.lastUtteredTime = Date.now();
            if (this.state === STATE.IDLE) {
                this.setState(STATE.LISTENING);
            }
        }
    }

    /**
     * Connect to WebSocket server
     */
    connectWebSocket() {
        return new Promise((resolve, reject) => {
            const subject = this.subjectSelect.value || '';
            const wsUrl = `ws://${CONFIG.WS_HOST}:${CONFIG.WS_PORT}/ws${subject ? `?subject=${encodeURIComponent(subject)}` : ''}`;

            console.log(`Connecting to ${wsUrl}`);
            this.ws = new WebSocket(wsUrl);
            this.ws.binaryType = 'arraybuffer';

            this.ws.onopen = () => {
                console.log('✓ WebSocket connected');
                resolve();
            };

            this.ws.onmessage = (evt) => {
                if (evt.data instanceof ArrayBuffer) {
                    this.onAudioChunk(evt.data);
                } else {
                    try {
                        const msg = JSON.parse(evt.data);
                        this.onServerMessage(msg);
                    } catch (err) {
                        console.warn('Failed to parse message:', evt.data);
                    }
                }
            };

            this.ws.onerror = (err) => {
                console.error('WebSocket error:', err);
                reject(new Error('WebSocket connection failed'));
            };

            this.ws.onclose = () => {
                console.log('WebSocket disconnected');
                this.stopSession();
            };

            setTimeout(() => {
                if (this.ws.readyState !== WebSocket.OPEN) {
                    reject(new Error('WebSocket connection timeout'));
                }
            }, 5000);
        });
    }

    /**
     * Handle text messages from server
     */
    onServerMessage(msg) {
        if (msg.type === 'transcript') {
            this.addTranscriptItem('student', msg.text);
            this.debug(`Transcript: ${msg.text}`);
        } else if (msg.type === 'response_start') {
            this.setState(STATE.PROCESSING);
        } else if (msg.type === 'response_chunk') {
            // Accumulate response for transcript display
            if (!this.currentResponse) {
                this.currentResponse = '';
            }
            this.currentResponse += msg.text;
        } else if (msg.type === 'response_end') {
            if (this.currentResponse) {
                this.addTranscriptItem('tutor', this.currentResponse);
                this.currentResponse = '';
            }
            // Return to listening state after audio finishes
            this.setState(STATE.LISTENING);
        } else if (msg.type === 'latency') {
            this.displayLatency(msg.ms);
        }
    }

    /**
     * Handle audio chunks received from server
     */
    onAudioChunk(arrayBuffer) {
        if (this.firstAudioTime === null) {
            this.firstAudioTime = Date.now();
            const latency = this.firstAudioTime - this.utteranceStartTime;
            this.displayLatency(latency);
            this.debug(`First audio received: ${latency}ms`);
        }

        // Queue audio for playback
        this.audioPlaybackQueue.push(arrayBuffer);

        if (!this.isPlaying) {
            this.playNextAudioChunk();
        }
    }

    /**
     * Play queued audio chunks sequentially
     */
    async playNextAudioChunk() {
        if (this.audioPlaybackQueue.length === 0) {
            this.isPlaying = false;
            this.firstAudioTime = null;
            return;
        }

        this.isPlaying = true;
        this.setState(STATE.SPEAKING);

        const arrayBuffer = this.audioPlaybackQueue.shift();

        try {
            const audioBuffer = await this.audioContext.decodeAudioData(arrayBuffer);
            const source = this.audioContext.createBufferSource();
            source.buffer = audioBuffer;
            source.connect(this.audioContext.destination);

            await new Promise((resolve) => {
                source.onended = resolve;
                source.start(0);
            });

            // Play next chunk
            this.playNextAudioChunk();
        } catch (err) {
            console.error('Failed to decode audio:', err);
            this.playNextAudioChunk();
        }
    }

    /**
     * Stop the current session
     */
    async stopSession() {
        this.isRecording = false;

        // Close WebSocket
        if (this.ws) {
            this.ws.send(JSON.stringify({ type: 'stop' }));
            this.ws.close();
            this.ws = null;
        }

        // Stop microphone
        if (this.mediaStream) {
            this.mediaStream.getTracks().forEach((track) => track.stop());
            this.mediaStream = null;
        }

        // Stop audio playback
        this.audioPlaybackQueue = [];

        // Update UI
        this.setState(STATE.IDLE);
        this.startBtn.textContent = 'Start Session';
        this.subjectSelect.disabled = false;
        this.latencyValue.textContent = '—';
        this.firstAudioTime = null;
        this.utteranceStartTime = null;
    }

    /**
     * Update application state and UI
     */
    setState(newState) {
        this.state = newState;

        const stateConfig = {
            [STATE.IDLE]: { dot: '', text: 'Ready', color: '' },
            [STATE.LISTENING]: { dot: 'listening', text: '🎤 Listening', color: 'listening' },
            [STATE.PROCESSING]: { dot: 'processing', text: '⚙️ Processing', color: 'processing' },
            [STATE.SPEAKING]: { dot: 'speaking', text: '🔊 Speaking', color: 'speaking' },
        };

        const config = stateConfig[newState];
        this.statusDot.className = `status-dot ${config.dot}`;
        this.statusText.textContent = config.text;
    }

    /**
     * Display latency value
     */
    displayLatency(ms) {
        this.latencyValue.textContent = `${Math.round(ms)}ms`;
        this.latencyValue.classList.add('active');
    }

    /**
     * Add a line to the transcript
     */
    addTranscriptItem(type, text) {
        if (this.transcriptList.querySelector('.welcome')) {
            this.transcriptList.innerHTML = '';
        }

        const item = document.createElement('div');
        item.className = `transcript-item ${type}`;
        item.innerHTML = `<p>${this.escapeHtml(text)}</p>`;
        this.transcriptList.appendChild(item);
        this.transcriptList.scrollTop = this.transcriptList.scrollHeight;
    }

    /**
     * Display error message
     */
    showError(message) {
        this.addTranscriptItem('error', `❌ Error: ${message}`);
        console.error(message);
    }

    /**
     * Debug logging
     */
    debug(message) {
        console.log(`[DEBUG] ${message}`);
        // Uncomment to show debug info in UI:
        // this.debugText.textContent = message;
    }

    /**
     * Escape HTML entities
     */
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    console.log('DOM loaded, initializing Cascade...');
    window.cascadeClient = new CascadeClient();
});
