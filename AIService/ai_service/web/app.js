const state = { setupRequired: false, me: null, sources: [], customers: [], frames: [], streams: [] }
let livePlayer
let liveRetryTimer

const $ = (selector) => document.querySelector(selector)
const $$ = (selector) => [...document.querySelectorAll(selector)]

function escapeHTML(value) {
    return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;')
}

function localTime(value) {
    if (!value) return '-'
    const date = new Date(value)
    return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false })
}

function sourceType(value) {
    return { screen: '电脑桌面', usb_camera: 'USB摄像头', ip_camera: 'RTSP/NVR', direct_camera: '摄像头直推' }[value] || value || '-'
}

function codecName(value) {
    const normalized = String(value || '').trim().toUpperCase()
    if (normalized === 'HEVC' || normalized === 'H265' || normalized === 'H.265') return 'H.265'
    if (normalized === 'AVC' || normalized === 'H264' || normalized === 'H.264') return 'H.264'
    return normalized || '-'
}

function isHEVC(value) { return codecName(value) === 'H.265' }

async function request(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) {
        const raw = body.error || `请求失败 HTTP ${response.status}`
        let message = raw
        const jsonStart = String(raw).indexOf('{')
        if (jsonStart >= 0) {
            try { message = JSON.parse(String(raw).slice(jsonStart)).error || raw } catch (_) { /* keep raw */ }
        }
        const error = new Error(message)
        error.status = response.status
        throw error
    }
    return body
}

function toast(message, error = false) {
    const node = $('#toast')
    node.textContent = message
    node.className = `toast show${error ? ' error' : ''}`
    clearTimeout(toast.timer)
    toast.timer = setTimeout(() => { node.className = 'toast' }, 3200)
}

function showAuth(setupRequired = false, message = '') {
    state.setupRequired = setupRequired
    $('#portalShell').classList.add('hidden')
    $('#authGate').classList.remove('hidden')
    $('#authTitle').textContent = setupRequired ? '初始化超级管理员' : '超级管理员登录'
    $('#authDescription').textContent = setupRequired
        ? '系统尚无账号，请先创建唯一的平台管理员。'
        : '此入口仅供超级管理员管理客户账号、设备归属和平台服务。'
    $('#authSubmit').textContent = setupRequired ? '创建管理员并进入平台' : '登录'
    $('#authPassword').autocomplete = setupRequired ? 'new-password' : 'current-password'
    $('#authMessage').textContent = message
}

function showPortal() {
    $('#authGate').classList.add('hidden')
    $('#portalShell').classList.remove('hidden')
    const user = state.me.user
    $('#accountName').textContent = user.customer_name || user.username
    $('#accountRole').textContent = user.role === 'platform_admin' ? `平台管理员 · ${user.username}` : `客户管理员 · ${user.username}`
    const admin = user.role === 'platform_admin'
    $('#customersTab').classList.toggle('hidden', !admin)
    $$('.admin-only').forEach((node) => node.classList.toggle('hidden', !admin))
}

function renderMetrics() {
    $('#onlineMetric').textContent = state.sources.filter((item) => item.active).length
    $('#recordingMetric').textContent = state.sources.filter((item) => item.recording_enabled).length
    $('#samplingMetric').textContent = state.sources.filter((item) => item.sampling_enabled).length
    $('#frameMetric').textContent = state.sources.reduce((sum, item) => sum + Number(item.frame_count || 0), 0)
}

function customerOptions(selectedID) {
    return `<option value="0">未分配</option>` + state.customers.map((customer) =>
        `<option value="${Number(customer.id)}" ${Number(selectedID) === Number(customer.id) ? 'selected' : ''}>${escapeHTML(customer.name)}</option>`
    ).join('')
}

function renderSources() {
    const tbody = $('#sourceRows')
    const admin = state.me.user.role === 'platform_admin'
    if (!state.sources.length) {
        tbody.innerHTML = `<tr><td colspan="${admin ? 9 : 8}" class="empty">当前账号还没有可管理的视频源。</td></tr>`
        renderMetrics()
        renderSourceFilter()
        return
    }
    tbody.innerHTML = state.sources.map((source) => `
        <tr data-source-id="${Number(source.video_source_id)}">
            <td><span class="source-name">${escapeHTML(source.display_name || source.stream_name)}</span><span class="source-meta">${escapeHTML(sourceType(source.source_type))} · ${escapeHTML(source.stream_name)}</span></td>
            ${admin ? `<td><select class="customer-select source-owner" data-current="${Number(source.customer_id || 0)}">${customerOptions(source.customer_id)}</select></td>` : ''}
            <td class="device-info-cell">
                <div class="operator-editor">
                    <input class="operator-name" maxlength="20" value="${escapeHTML(source.operator_name || '')}" placeholder="填写负责人姓名或编号" aria-label="点位负责人" />
                    <button class="ghost operator-save" type="button">保存</button>
                </div>
                <dl class="device-meta-grid">
                    <div><dt>主机名</dt><dd title="${escapeHTML(source.hostname || '未上报')}">${escapeHTML(source.hostname || '未上报')}</dd></div>
                    <div><dt>内网 IP</dt><dd title="${escapeHTML(source.local_ip || '未上报')}">${escapeHTML(source.local_ip || '未上报')}</dd></div>
                    <div class="device-meta-wide"><dt>MAC 地址</dt><dd title="${escapeHTML(source.mac || '未上报')}">${escapeHTML(source.mac || '未上报')}</dd></div>
                </dl>
            </td>
            <td><span class="pill ${source.active ? 'online' : 'offline'}">${source.active ? '在线' : '离线'}</span></td>
            <td><input class="recording-enabled" type="checkbox" ${source.recording_enabled ? 'checked' : ''} aria-label="开启录像" /></td>
            <td><input class="compact-input retain-hours" type="number" min="1" max="87600" value="${Number(source.recording_retain_hours || 48)}" /> 小时</td>
            <td><input class="sampling-enabled" type="checkbox" ${source.sampling_enabled ? 'checked' : ''} aria-label="开启实时抽帧" /></td>
            <td class="sampling-config">每 <input class="compact-input sample-interval" type="number" min="1" max="1440" value="${Number(source.sampling_interval_minutes || 1)}" /> 分钟 <input class="compact-input sample-count" type="number" min="1" value="${Number(source.sampling_frame_count || 2)}" /> 帧</td>
            <td>${Number(source.frame_count || 0)} 张<br><span class="source-meta">${escapeHTML(localTime(source.last_captured_at))}</span></td>
        </tr>`).join('')
    $$('.source-owner').forEach((select) => select.addEventListener('change', assignOwner))
    $$('.operator-save').forEach((button) => button.addEventListener('click', saveOperatorName))
    renderMetrics()
    renderSourceFilter()
}

function renderSourceFilter() {
    const select = $('#resultSource')
    const current = select.value
    select.innerHTML = '<option value="">全部视频源</option>' + state.sources.map((source) =>
        `<option value="${escapeHTML(source.stream_name)}">${escapeHTML(source.display_name || source.stream_name)}</option>`
    ).join('')
    if ([...select.options].some((option) => option.value === current)) select.value = current
}

function renderCustomers() {
    const list = $('#customerList')
    if (!state.customers.length) {
        list.innerHTML = '<p class="empty">尚未创建客户账号。</p>'
        return
    }
    list.innerHTML = state.customers.map((customer) => `
        <article class="customer-item">
            <div><strong>${escapeHTML(customer.name)}</strong><span>登录账号 ${escapeHTML(customer.username || '-')} · 客户编号 ${Number(customer.id)} · ${customer.enabled ? '正常' : '已停用'}</span></div>
            <div class="customer-actions">
                <button class="secondary customer-reset" data-id="${Number(customer.id)}" type="button">重置密码</button>
                <button class="secondary customer-toggle" data-id="${Number(customer.id)}" data-enabled="${customer.enabled ? '1' : '0'}" type="button">${customer.enabled ? '停用' : '启用'}</button>
            </div>
        </article>`
    ).join('')
    $$('.customer-toggle').forEach((button) => button.addEventListener('click', toggleCustomer))
    $$('.customer-reset').forEach((button) => button.addEventListener('click', resetCustomerPassword))
}

async function toggleCustomer(event) {
    const button = event.currentTarget
    const enabled = button.dataset.enabled !== '1'
    button.disabled = true
    try {
        await request('/api/dashboard/customers', { method: 'PUT', body: JSON.stringify({ customer_id: Number(button.dataset.id), enabled }) })
        toast(enabled ? '客户账号已启用' : '客户账号已停用，已有会话已退出')
        await loadCustomers()
    } catch (error) { toast(error.message, true); button.disabled = false }
}

async function resetCustomerPassword(event) {
    const button = event.currentTarget
    const password = window.prompt('请输入新的客户密码（8～72位）')
    if (password === null) return
    if (password.length < 8 || password.length > 72) return toast('密码长度必须为8～72位', true)
    button.disabled = true
    try {
        await request('/api/dashboard/customers', { method: 'PUT', body: JSON.stringify({ customer_id: Number(button.dataset.id), new_password: password }) })
        toast('客户密码已重置，已有会话已退出')
    } catch (error) { toast(error.message, true) } finally { button.disabled = false }
}

function selectedLiveStream() {
    return state.streams.find((stream) => stream.stream_name === $('#liveSource').value)
}

function updateLiveMetadata(stream) {
    $('#liveCodec').textContent = `编码 ${stream ? codecName(stream.codec) : '-'}`
    $('#liveResolution').textContent = stream?.width && stream?.height
        ? `分辨率 ${Number(stream.width)} × ${Number(stream.height)}` : '分辨率 -'
}

function setLiveStatus(message, error = false) {
    const status = $('#liveStatus')
    status.textContent = message
    status.className = `message${error ? ' error' : ''}`
}

function showLivePlaceholder(title, detail = '') {
    const placeholder = $('#livePlaceholder')
    placeholder.classList.remove('hidden')
    placeholder.firstElementChild.textContent = title
    placeholder.lastElementChild.textContent = detail
}

function hideLivePlaceholder() { $('#livePlaceholder').classList.add('hidden') }

function destroyLivePlayer() {
    clearTimeout(liveRetryTimer)
    liveRetryTimer = undefined
    if (livePlayer) {
        try {
            livePlayer.pause()
            livePlayer.unload()
            livePlayer.detachMediaElement()
            livePlayer.destroy()
        } catch (error) { console.warn('[live] 销毁播放器失败', error) }
        livePlayer = undefined
    }
    const video = $('#liveVideo')
    video.onplaying = null
    video.onwaiting = null
    video.removeAttribute('src')
    video.load()
}

function renderLiveSources() {
    const select = $('#liveSource')
    const previous = select.value
    if (!state.streams.length) {
        select.innerHTML = '<option value="">当前没有在线流</option>'
        updateLiveMetadata(null)
        return
    }
    select.innerHTML = state.streams.map((stream) => {
        const details = [codecName(stream.codec), stream.width && stream.height ? `${Number(stream.width)}×${Number(stream.height)}` : ''].filter(Boolean).join(' · ')
        return `<option value="${escapeHTML(stream.stream_name)}">${escapeHTML(stream.display_name || stream.stream_name)}（${escapeHTML(details)}）</option>`
    }).join('')
    if (state.streams.some((stream) => stream.stream_name === previous)) select.value = previous
    updateLiveMetadata(selectedLiveStream())
}

async function startLivePlayback() {
    const stream = selectedLiveStream()
    destroyLivePlayer()
    updateLiveMetadata(stream)
    if (!stream) {
        setLiveStatus('当前没有可播放的在线流')
        showLivePlaceholder('当前没有在线流', '请确认客户端或摄像头已经推流到SRS')
        return
    }
    if (!stream.playback_url) {
        setLiveStatus('SRS网页播放地址未配置', true)
        showLivePlaceholder('播放地址不可用', '请配置AI_SRS_PUBLIC_BASE')
        return
    }
    if (!window.mpegts) {
        setLiveStatus('mpegts.js加载失败', true)
        showLivePlaceholder('播放器加载失败', '请检查AIService播放器资源是否完整')
        return
    }
    const features = window.mpegts.getFeatureList()
    if (!features.mseLivePlayback) {
        setLiveStatus('当前Chrome不支持MSE实时流播放', true)
        showLivePlaceholder('浏览器不支持实时播放', '请升级Chrome并启用硬件加速')
        return
    }
    if (isHEVC(stream.codec) && !features.mseH265Playback) {
        setLiveStatus('当前电脑不支持H.265硬件解码', true)
        showLivePlaceholder('无法播放H.265', '请启用Chrome硬件加速、更新显卡驱动，或使用H.264子码流')
        return
    }
    const video = $('#liveVideo')
    showLivePlaceholder('正在连接实时视频…', `${codecName(stream.codec)} · HTTP-FLV`)
    setLiveStatus('正在连接SRS…')
    const player = window.mpegts.createPlayer(
        { type: 'flv', isLive: true, hasAudio: false, hasVideo: true, url: stream.playback_url },
        { enableWorker: false, enableStashBuffer: false, stashInitialSize: 128, lazyLoad: false, autoCleanupSourceBuffer: true, autoCleanupMaxBackwardDuration: 10, autoCleanupMinBackwardDuration: 5, liveBufferLatencyChasing: true, liveBufferLatencyMaxLatency: 1.5, liveBufferLatencyMinRemain: 0.3 },
    )
    livePlayer = player
    video.onplaying = () => {
        if (livePlayer !== player) return
        hideLivePlaceholder()
        setLiveStatus(`正在播放 ${stream.display_name || stream.stream_name}`)
    }
    video.onwaiting = () => { if (livePlayer === player) setLiveStatus('网络缓冲中…') }
    player.on(window.mpegts.Events.ERROR, (type, detail, info) => {
        if (livePlayer !== player) return
        setLiveStatus(`播放中断：${String(info?.msg || detail || type || '未知错误')}`, true)
        showLivePlaceholder('视频连接中断', '3秒后自动重连')
        clearTimeout(liveRetryTimer)
        liveRetryTimer = setTimeout(() => {
            if ($('#livePage').classList.contains('active') && $('#liveSource').value === stream.stream_name) {
                startLivePlayback().catch((error) => setLiveStatus(error.message, true))
            }
        }, 3000)
    })
    player.attachMediaElement(video)
    player.load()
    await player.play().catch(() => {})
}

async function loadStreams(autoplay = false) {
    const payload = await request('/api/dashboard/streams')
    state.streams = Array.isArray(payload.streams) ? payload.streams.filter((stream) => stream?.active !== false && stream?.stream_name) : []
    renderLiveSources()
    if (autoplay) await startLivePlayback()
}

function renderFrames() {
    const grid = $('#resultGrid')
    if (!state.frames.length) {
        grid.innerHTML = '<p class="empty">当前筛选条件下还没有实时抽帧结果。</p>'
        return
    }
    const names = new Map(state.sources.map((source) => [source.stream_name, source.display_name]))
    grid.innerHTML = state.frames.map((frame) => `
        <article class="result-card">
            <a href="/api/dashboard/frames/${Number(frame.id)}/image" target="_blank" rel="noopener"><img src="/api/dashboard/frames/${Number(frame.id)}/image" loading="lazy" alt="实时抽帧结果" /></a>
            <div class="result-info"><strong>${escapeHTML(names.get(frame.stream_name) || frame.display_name || frame.stream_name)}</strong><span>${escapeHTML(localTime(frame.captured_at))}</span><span>实时截图 · ${(Number(frame.file_size || 0) / 1024).toFixed(1)} KB</span></div>
        </article>`).join('')
}

async function loadSources() {
    const payload = await request('/api/dashboard/sources')
    state.sources = Array.isArray(payload.sources) ? payload.sources : []
    renderSources()
}

async function loadCustomers() {
    if (state.me.user.role !== 'platform_admin') return
    const payload = await request('/api/dashboard/customers')
    state.customers = Array.isArray(payload.customers) ? payload.customers : []
    renderCustomers()
}

async function saveConfigs() {
    const sources = $$('#sourceRows tr[data-source-id]').map((row) => ({
        video_source_id: Number(row.dataset.sourceId),
        recording_enabled: row.querySelector('.recording-enabled').checked,
        recording_retain_hours: Number.parseInt(row.querySelector('.retain-hours').value, 10),
        sampling_enabled: row.querySelector('.sampling-enabled').checked,
        sampling_interval_minutes: Number.parseInt(row.querySelector('.sample-interval').value, 10),
        sampling_frame_count: Number.parseInt(row.querySelector('.sample-count').value, 10),
    }))
    if (!sources.length) return toast('当前没有可保存的视频源', true)
    if (sources.some((item) => (item.recording_enabled && (!Number.isInteger(item.recording_retain_hours) || item.recording_retain_hours < 1 || item.recording_retain_hours > 87600)) || !Number.isInteger(item.sampling_interval_minutes) || item.sampling_interval_minutes < 1 || item.sampling_interval_minutes > 1440 || !Number.isInteger(item.sampling_frame_count) || item.sampling_frame_count < 1 || item.sampling_frame_count > item.sampling_interval_minutes * 60)) {
        return toast('录像保留应为1～87600小时；抽帧间隔应为1～1440分钟，且平均频率不能超过每分钟60帧', true)
    }
    const button = $('#saveConfigsButton')
    button.disabled = true
    try {
        await request('/api/dashboard/sources', { method: 'PUT', body: JSON.stringify({ sources }) })
        $('#saveMessage').className = 'message success'
        $('#saveMessage').textContent = '所有视频源配置已保存并立即应用。'
        toast('配置已保存')
        await loadSources()
    } catch (error) {
        $('#saveMessage').className = 'message error'
        $('#saveMessage').textContent = error.message
        toast(error.message, true)
    } finally { button.disabled = false }
}

async function assignOwner(event) {
    const select = event.currentTarget
    const row = select.closest('tr')
    try {
        await request('/api/dashboard/source-owner', { method: 'PUT', body: JSON.stringify({ video_source_id: Number(row.dataset.sourceId), customer_id: Number(select.value) }) })
        toast('视频源所属客户已更新，服务开关已重置')
        await loadSources()
    } catch (error) {
        select.value = select.dataset.current
        toast(error.message, true)
    }
}

async function saveOperatorName(event) {
    const button = event.currentTarget
    const row = button.closest('tr')
    const input = row.querySelector('.operator-name')
    const operatorName = input.value.trim()
    if (!operatorName || [...operatorName].length > 20) {
        return toast('点位负责人必须为1～20个字符', true)
    }
    button.disabled = true
    try {
        await request('/api/dashboard/source-operator', {
            method: 'PUT',
            body: JSON.stringify({
                video_source_id: Number(row.dataset.sourceId),
                operator_name: operatorName,
            }),
        })
        toast('点位负责人已更新')
        await loadSources()
    } catch (error) {
        toast(error.message, true)
    } finally { button.disabled = false }
}

async function loadFrames() {
    const params = new URLSearchParams()
    if ($('#resultSource').value) params.set('stream_name', $('#resultSource').value)
    if ($('#resultDate').value) params.set('date', $('#resultDate').value)
    $('#resultGrid').innerHTML = '<p class="empty">正在读取实时抽帧结果…</p>'
    state.frames = await request(`/api/dashboard/frames?${params}`)
    renderFrames()
}

async function submitAuth(event) {
    event.preventDefault()
    const path = state.setupRequired ? '/api/dashboard/auth/setup' : '/api/dashboard/auth/login'
    $('#authSubmit').disabled = true
    $('#authMessage').textContent = ''
    try {
        const payload = await request(path, { method: 'POST', body: JSON.stringify({ username: $('#authUsername').value.trim(), password: $('#authPassword').value }) })
        state.me = payload
        await enterPortal()
    } catch (error) {
        $('#authMessage').className = 'message error'
        $('#authMessage').textContent = error.message
    } finally { $('#authSubmit').disabled = false }
}

async function createCustomer(event) {
    event.preventDefault()
    const form = event.currentTarget
    try {
        await request('/api/dashboard/customers', { method: 'POST', body: JSON.stringify({ name: $('#customerName').value.trim(), username: $('#customerUsername').value.trim(), password: $('#customerPassword').value }) })
        form.reset()
        toast('客户账号已创建')
        await loadCustomers()
        renderSources()
    } catch (error) { toast(error.message, true) }
}

async function enterPortal() {
    if (state.me?.user?.role !== 'platform_admin') {
        try { await request('/api/dashboard/auth/logout', { method: 'POST', body: '{}' }) } catch (_) { /* ignore */ }
        state.me = null
        showAuth(false, '此入口仅供超级管理员使用，请从客户移动平台登录。')
        return
    }
    showPortal()
    await loadCustomers()
    await loadSources()
    await loadStreams()
}

function bindEvents() {
    $('#authForm').addEventListener('submit', submitAuth)
    $('#logoutButton').addEventListener('click', async () => {
        try { await request('/api/dashboard/auth/logout', { method: 'POST', body: '{}' }) } catch (_) { /* clear local view anyway */ }
        state.me = null
        destroyLivePlayer()
        showAuth(false)
    })
    $('#saveConfigsButton').addEventListener('click', saveConfigs)
    $('#refreshResultsButton').addEventListener('click', () => loadFrames().catch((error) => toast(error.message, true)))
    $('#refreshStreamsButton').addEventListener('click', () => loadStreams(true).catch((error) => toast(error.message, true)))
    $('#liveSource').addEventListener('change', () => startLivePlayback().catch((error) => setLiveStatus(error.message, true)))
    $('#customerForm').addEventListener('submit', createCustomer)
    $$('.tab').forEach((button) => button.addEventListener('click', () => {
        $$('.tab').forEach((tab) => tab.classList.toggle('active', tab === button))
        $$('.page').forEach((page) => page.classList.remove('active'))
        $(`#${button.dataset.page}Page`).classList.add('active')
        if (button.dataset.page === 'results') loadFrames().catch((error) => toast(error.message, true))
        if (button.dataset.page === 'live') {
            loadStreams(true).catch((error) => {
                setLiveStatus(error.message, true)
                showLivePlaceholder('读取在线流失败', error.message)
                toast(error.message, true)
            })
        } else { destroyLivePlayer() }
    }))
    window.addEventListener('beforeunload', destroyLivePlayer)
}

async function initialize() {
    bindEvents()
    try {
        const status = await request('/api/dashboard/auth/status')
        if (status.setup_required) return showAuth(true)
        try {
            state.me = await request('/api/dashboard/auth/me')
            await enterPortal()
        } catch (_) { showAuth(false) }
    } catch (error) { showAuth(false, error.message) }
}

initialize()
