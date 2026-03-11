import './style.css';

// ==================== CONSTANTS ====================
const POLL_MS = 10000;
const PET_POLL_MS = 60000;
const $ = (id) => document.getElementById(id);

// ==================== STATE ====================
let token = localStorage.getItem('hoya_token') || '';
let setupComplete = false;
let chartHours = 6;
const chartAbortControllers = {};

// ==================== TEXT CLEANUP ====================
// Remove **bold**, *italic*, [source](url), bare URLs, and citation marks from AI text
function cleanText(txt) {
    if (!txt) return '';
    return txt
        .replace(/\*\*([^*]+)\*\*/g, '$1')   // **bold** -> bold
        .replace(/\*([^*]+)\*/g, '$1')        // *italic* -> italic
        .replace(/\[([^\]]+)\]\([^)]+\)/g, '') // [text](url) -> remove
        .replace(/https?:\/\/[^\s)]+/g, '')    // bare URLs -> remove
        .replace(/【[^】]*】/g, '')              // 【source】 -> remove
        .replace(/\s{2,}/g, ' ')               // collapse whitespace
        .trim();
}

// ==================== TIME HELPERS ====================
function timeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);
    if (seconds < 60) return 'agora mesmo';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `há ${minutes} min`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `há ${hours}h`;
    const days = Math.floor(hours / 24);
    return `há ${days}d`;
}

// ==================== API HELPER ====================
async function api(method, url, body = null, isFormData = false) {
    const headers = {};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    if (body && !isFormData) headers['Content-Type'] = 'application/json';

    const opts = { method, headers };
    if (body) opts.body = isFormData ? body : JSON.stringify(body);

    const res = await fetch(url, opts);
    if (res.status === 401) {
        token = '';
        localStorage.removeItem('hoya_token');
        showPage('login');
        const err = await res.json().catch(() => ({ detail: 'Não autorizado' }));
        throw new Error(err.detail || 'Não autorizado');
    }
    if (res.status === 429) {
        const err = await res.json().catch(() => ({ detail: 'Muitas tentativas' }));
        throw new Error(err.detail || 'Muitas tentativas. Aguarde.');
    }
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Erro desconhecido' }));
        throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
}

// ==================== ROUTER ====================
function showPage(name) {
    ['pageLanding', 'pageLogin', 'pageSignup', 'pageVerify', 'pageForgot', 'pageReset', 'pageSetup', 'pageDash'].forEach(id => {
        $(id).style.display = 'none';
    });
    if (name === 'landing') {
        $('pageLanding').style.display = 'block';
        initLandingObserver();
    }
    else if (name === 'login') $('pageLogin').style.display = 'flex';
    else if (name === 'signup') $('pageSignup').style.display = 'flex';
    else if (name === 'verify') $('pageVerify').style.display = 'flex';
    else if (name === 'forgot') $('pageForgot').style.display = 'flex';
    else if (name === 'reset') $('pageReset').style.display = 'flex';
    else if (name === 'setup') {
        $('pageSetup').style.display = 'block';
        checkCancelSetup();
    }
    else if (name === 'dash') $('pageDash').style.display = 'block';
}

async function checkCancelSetup() {
    try {
        $('cancelSetup1Btn').style.display = 'block';
        $('cancelSetup2Btn').style.display = 'block';

        const cancelHandler = async () => {
            const data = await api('GET', `/api/plants?_=${Date.now()}`);
            // Check if user has at least 1 configured plant other than the current active slot
            const otherConfiguredPlants = data.ok && data.plants.some(p => p.id !== data.active_slot);
            if (otherConfiguredPlants) {
                // User already has pet plants — go back to dashboard, not login
                if (!confirm("Cancelar a adição desta nova planta?")) return;
                try {
                    await api('DELETE', `/api/plants/${data.active_slot}`);
                } catch (e) {
                    console.error('Delete slot failed:', e);
                    // Slot may be empty/unlistable — switch active back to a configured plant
                    const firstConfigured = data.plants.find(p => p.id !== data.active_slot);
                    if (firstConfigured) {
                        try { await api('GET', `/api/plants/${firstConfigured.id}/switch`); } catch (_) {}
                    }
                }
                dashInitialized = false;
                checkSetupAndRoute();
            } else {
                // First plant setup — go back to dashboard
                if (!confirm("Cancelar a configuração e voltar ao início?")) return;
                dashInitialized = false;
                checkSetupAndRoute();
            }
        };

        $('cancelSetup1Btn').onclick = cancelHandler;
        $('cancelSetup2Btn').onclick = cancelHandler;
    } catch (e) {
        $('cancelSetup1Btn').style.display = 'block';
        $('cancelSetup2Btn').style.display = 'block';
        const fallback = () => {
            token = '';
            localStorage.removeItem('hoya_token');
            window.location.reload();
        };
        $('cancelSetup1Btn').onclick = fallback;
        $('cancelSetup2Btn').onclick = fallback;
    }
}

// ==================== LOADING OVERLAY ====================
function showLoading(text = 'Carregando...') {
    let overlay = document.querySelector('.loading-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.className = 'loading-overlay';
        overlay.innerHTML = `<div class="loading-spinner"></div><div class="loading-text">${text}</div>`;
        document.body.appendChild(overlay);
    } else {
        overlay.querySelector('.loading-text').textContent = text;
        overlay.style.display = 'flex';
    }
}
function hideLoading() {
    const overlay = document.querySelector('.loading-overlay');
    if (overlay) overlay.style.display = 'none';
}

// ==================== BUTTON LOADING STATE ====================
function btnLoading(btn, loading) {
    const text = btn.querySelector('.btn-text');
    const loader = btn.querySelector('.btn-loader');
    if (loading) {
        if (text) text.style.display = 'none';
        if (loader) loader.style.display = 'block';
        btn.disabled = true;
    } else {
        if (text) text.style.display = 'inline';
        if (loader) loader.style.display = 'none';
        btn.disabled = false;
    }
}

// ==================== AUTH/LOGIN/SIGNUP/VERIFY ====================
let tempEmailForVerify = '';

function initAuth() {
    // Navigation Links
    if ($('showSignupLink')) {
        $('showSignupLink').addEventListener('click', (e) => { e.preventDefault(); showPage('signup'); });
    }
    if ($('showLoginLink')) {
        $('showLoginLink').addEventListener('click', (e) => { e.preventDefault(); showPage('login'); });
    }
    if ($('showLoginFromVerifyLink')) {
        $('showLoginFromVerifyLink').addEventListener('click', (e) => { e.preventDefault(); showPage('login'); });
    }
    if ($('showForgotLink')) {
        $('showForgotLink').addEventListener('click', (e) => { e.preventDefault(); showPage('forgot'); });
    }
    if ($('showLoginFromForgotLink')) {
        $('showLoginFromForgotLink').addEventListener('click', (e) => { e.preventDefault(); showPage('login'); });
    }
    if ($('showLoginFromResetLink')) {
        $('showLoginFromResetLink').addEventListener('click', (e) => { e.preventDefault(); showPage('login'); });
    }

    // Password Visibility Toggles
    const togglePass = (inputId, btnId) => {
        const input = $(inputId);
        const btn = $(btnId);
        if (input && btn) {
            btn.addEventListener('click', () => {
                const type = input.getAttribute('type') === 'password' ? 'text' : 'password';
                input.setAttribute('type', type);

                // Update SVG icon (eye open/closed)
                if (type === 'text') {
                    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0112 20c-7 0-11-8-11-8a18.45 18.45 0 015.06-5.94M9.9 4.24A9.12 9.12 0 0112 4c7 0 11 8 11 8a18.5 18.5 0 01-2.16 3.19m-6.72-1.07a3 3 0 11-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>`;
                } else {
                    btn.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>`;
                }
            });
        }
    };
    togglePass('loginPass', 'toggleLoginPass');
    togglePass('signupPass', 'toggleSignupPass');
    togglePass('resetPass', 'toggleResetPass');

    // Login Form
    if ($('loginForm')) {
        $('loginForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = $('loginBtn');
            const errEl = $('loginError');
            errEl.textContent = '';
            btnLoading(btn, true);

            try {
                const data = await api('POST', '/api/login', {
                    email: $('loginEmail').value.trim(),
                    password: $('loginPass').value,
                });
                token = data.token;
                localStorage.setItem('hoya_token', token);
                await checkSetupAndRoute();
            } catch (err) {
                if (err.message === "verification_required") {
                    tempEmailForVerify = $('loginEmail').value.trim();
                    if ($('verifySubtitle')) $('verifySubtitle').textContent = `Sua conta precisa ser ativada. Um código foi enviado para ${tempEmailForVerify} no cadastro.`;
                    showPage('verify');
                } else {
                    errEl.textContent = err.message || 'Falha no login';
                }
            } finally {
                btnLoading(btn, false);
            }
        });
    }

    // Signup Form
    if ($('signupForm')) {
        $('signupForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = $('signupBtn');
            const errEl = $('signupError');
            errEl.textContent = '';
            btnLoading(btn, true);

            const email = $('signupEmail').value.trim();
            try {
                await api('POST', '/api/register', {
                    email: email,
                    password: $('signupPass').value,
                });
                // Success! Save email and go to verify screen
                tempEmailForVerify = email;
                if ($('verifySubtitle')) $('verifySubtitle').textContent = `Enviamos um código para ${email}.`;
                if ($('verifyCode')) $('verifyCode').value = '';
                showPage('verify');
            } catch (err) {
                errEl.textContent = err.message || 'Falha no cadastro.';
            } finally {
                btnLoading(btn, false);
            }
        });
    }

    // Verify Form
    if ($('verifyForm')) {
        $('verifyForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = $('verifyBtn');
            const errEl = $('verifyError');
            errEl.textContent = '';
            btnLoading(btn, true);

            try {
                const data = await api('POST', '/api/verify', {
                    email: tempEmailForVerify,
                    code: $('verifyCode').value.trim(),
                });
                // Verification success, token received
                token = data.token;
                localStorage.setItem('hoya_token', token);
                await checkSetupAndRoute();
            } catch (err) {
                errEl.textContent = err.message || 'Código incorreto.';
            } finally {
                btnLoading(btn, false);
            }
        });
    }

    // Forgot Password Form
    if ($('forgotForm')) {
        $('forgotForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = $('forgotBtn');
            const errEl = $('forgotError');
            errEl.textContent = '';
            btnLoading(btn, true);

            const email = $('forgotEmail').value.trim();
            try {
                await api('POST', '/api/password/forgot', { email: email });
                // Success! Always show reset screen
                tempEmailForVerify = email;
                if ($('resetSubtitle')) $('resetSubtitle').textContent = `Enviamos um código para ${email}.`;
                if ($('resetCode')) $('resetCode').value = '';
                if ($('resetPass')) $('resetPass').value = '';
                showPage('reset');
            } catch (err) {
                errEl.textContent = err.message || 'Erro ao solicitar código.';
            } finally {
                btnLoading(btn, false);
            }
        });
    }

    // Reset Password Form
    if ($('resetForm')) {
        $('resetForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const btn = $('resetBtn');
            const errEl = $('resetError');
            errEl.textContent = '';
            btnLoading(btn, true);

            try {
                const data = await api('POST', '/api/password/reset', {
                    email: tempEmailForVerify,
                    code: $('resetCode').value.trim(),
                    new_password: $('resetPass').value
                });
                // Success, auto-login
                token = data.token;
                localStorage.setItem('hoya_token', token);
                await checkSetupAndRoute();
            } catch (err) {
                errEl.textContent = err.message || 'Código incorreto ou expirado.';
            } finally {
                btnLoading(btn, false);
            }
        });
    }
}

// ==================== SETUP ====================
let selectedFile = null;
let selectedPetType = '';
let selectedPetFile = null;

function initSetup() {
    const cameraInput = $('plantCameraInput');
    const galleryInput = $('plantGalleryInput');
    const uploadArea = $('uploadArea');
    const previewWrap = $('uploadPreview');
    const previewImg = $('previewImg');
    const analyzeBtn = $('analyzePlantBtn');

    // Camera button
    $('btnCamera').addEventListener('click', () => cameraInput.click());
    // Gallery button
    $('btnGallery').addEventListener('click', () => galleryInput.click());

    // Handle file from either source
    function handleFile(e) {
        const file = e.target.files[0];
        if (!file) return;
        selectedFile = file;
        previewImg.src = URL.createObjectURL(file);
        uploadArea.style.display = 'none';
        previewWrap.style.display = 'block';
        analyzeBtn.style.display = 'flex';
    }
    cameraInput.addEventListener('change', handleFile);
    galleryInput.addEventListener('change', handleFile);

    $('changePhotoBtn').addEventListener('click', () => {
        selectedFile = null;
        uploadArea.style.display = 'grid';
        previewWrap.style.display = 'none';
        analyzeBtn.style.display = 'none';
        $('plantResult').style.display = 'none';
        cameraInput.value = '';
        galleryInput.value = '';
    });

    // Analyze plant
    analyzeBtn.addEventListener('click', async () => {
        if (!selectedFile) return;
        btnLoading(analyzeBtn, true);
        showLoading('Analisando planta...');

        try {
            const formData = new FormData();
            formData.append('file', selectedFile);
            const data = await api('POST', '/api/setup/plant', formData, true);
            const p = data.profile;

            $('plantNamePopular').textContent = p.nome_popular || '—';
            $('plantNameScientific').textContent = p.nome_cientifico || '—';
            $('plantDescription').textContent = cleanText(p.descricao_curta) || '—';
            $('rangeTemp').textContent = `${p.temperatura_ideal_min}–${p.temperatura_ideal_max}°C`;
            $('rangeHum').textContent = `${p.umidade_ar_ideal_min}–${p.umidade_ar_ideal_max}%`;
            $('rangeSoil').textContent = `${p.umidade_solo_ideal_min}–${p.umidade_solo_ideal_max}%`;
            $('plantCare').textContent = cleanText(p.cuidados_especiais) || '—';

            analyzeBtn.style.display = 'none';
            $('plantResult').style.display = 'block';
        } catch (err) {
            alert('Erro ao analisar: ' + err.message);
        } finally {
            btnLoading(analyzeBtn, false);
            hideLoading();
        }
    });

    // Go to step 2
    $('goToStep2Btn').addEventListener('click', () => {
        $('setupStep1').style.display = 'none';
        $('setupStep2').style.display = 'block';
    });

    // Pet selector
    document.querySelectorAll('.pet-option').forEach(opt => {
        opt.addEventListener('click', () => {
            document.querySelectorAll('.pet-option').forEach(o => o.classList.remove('selected'));
            opt.classList.add('selected');
            selectedPetType = opt.dataset.type;
            checkFinishBtn();
        });
    });

    $('petNameInput').addEventListener('input', checkFinishBtn);

    function checkFinishBtn() {
        const name = $('petNameInput').value.trim();
        $('finishSetupBtn').disabled = !(selectedPetType && name.length >= 1);
    }

    // Pet photo upload handlers
    $('btnPetCamera').addEventListener('click', () => $('petCameraInput').click());
    $('btnPetGallery').addEventListener('click', () => $('petGalleryInput').click());

    function handlePetFile(e) {
        const file = e.target.files[0];
        if (!file) return;
        selectedPetFile = file;
        $('petPreviewImg').src = URL.createObjectURL(file);
        $('petUploadArea').style.display = 'none';
        $('petUploadPreview').style.display = 'block';
    }
    $('petCameraInput').addEventListener('change', handlePetFile);
    $('petGalleryInput').addEventListener('change', handlePetFile);

    $('changePetPhotoBtn').addEventListener('click', () => {
        selectedPetFile = null;
        $('petUploadArea').style.display = 'grid';
        $('petUploadPreview').style.display = 'none';
        $('petCameraInput').value = '';
        $('petGalleryInput').value = '';
    });

    // Finish setup
    $('finishSetupBtn').addEventListener('click', async () => {
        const btn = $('finishSetupBtn');
        btnLoading(btn, true);
        showLoading('Configurando pet...');

        try {
            // Upload pet reference photo if provided
            if (selectedPetFile) {
                showLoading('Enviando foto do pet...');
                const petFormData = new FormData();
                petFormData.append('file', selectedPetFile);
                await api('POST', '/api/pet/upload-photo', petFormData, true);
            }

            await api('POST', '/api/pet/configure', {
                name: $('petNameInput').value.trim(),
                type: selectedPetType,
            });

            // Trigger first pet generation
            showLoading('Gerando primeira imagem do pet...');
            try {
                await api('POST', '/api/pet/generate');
            } catch (e) {
                // Non-fatal: image will be generated by scheduler
                console.warn('First pet generation failed:', e);
            }

            hideLoading();
            setupComplete = true;
            showPage('dash');
            initDashboard();
        } catch (err) {
            alert('Erro: ' + err.message);
        } finally {
            btnLoading(btn, false);
            hideLoading();
        }
    });
}

// ==================== PET CAROUSEL ====================
function petTypeIcon(type) {
    if (type === 'dog') {
        return `<svg class="plant-slot-pet-icon" width="16" height="16" viewBox="0 0 48 48" fill="none">
            <ellipse cx="24" cy="30" rx="10" ry="8" stroke="currentColor" stroke-width="3"/>
            <ellipse cx="14" cy="22" rx="4" ry="6" stroke="currentColor" stroke-width="3" fill="none"/>
            <ellipse cx="34" cy="22" rx="4" ry="6" stroke="currentColor" stroke-width="3" fill="none"/>
            <circle cx="20" cy="28" r="2" fill="currentColor"/>
            <circle cx="28" cy="28" r="2" fill="currentColor"/>
            <ellipse cx="24" cy="32" rx="3" ry="2" fill="currentColor"/>
        </svg>`;
    }
    return `<svg class="plant-slot-pet-icon" width="16" height="16" viewBox="0 0 48 48" fill="none">
        <path d="M12 36c0-8 5-16 12-16s12 8 12 16" stroke="currentColor" stroke-width="3"/>
        <path d="M14 20l-4-10 8 6" fill="currentColor"/>
        <path d="M34 20l4-10-8 6" fill="currentColor"/>
        <circle cx="20" cy="28" r="2" fill="currentColor"/>
        <circle cx="28" cy="28" r="2" fill="currentColor"/>
        <path d="M22 32c1 1 3 1 4 0" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
    </svg>`;
}

async function deletePlant(slotId, plantName) {
    if (!confirm(`Remover "${plantName}" e seu pet? Esta acao nao pode ser desfeita.`)) return;
    try {
        await api('DELETE', `/api/plants/${slotId}`);
        dashInitialized = false;
        clearInterval(pollTimer);
        clearInterval(petPollTimer);
        initDashboard();
    } catch (err) {
        alert(err.message);
    }
}

function resetSetupForm() {
    $('setupStep1').style.display = 'block';
    $('setupStep2').style.display = 'none';
    $('uploadArea').style.display = 'grid';
    $('uploadPreview').style.display = 'none';
    $('analyzePlantBtn').style.display = 'none';
    $('plantResult').style.display = 'none';
    $('plantCameraInput').value = '';
    $('plantGalleryInput').value = '';
    selectedFile = null;
    selectedPetType = '';
    selectedPetFile = null;
    $('petNameInput').value = '';
    $('finishSetupBtn').disabled = true;
    $('petUploadArea').style.display = 'grid';
    $('petUploadPreview').style.display = 'none';
    $('petCameraInput').value = '';
    $('petGalleryInput').value = '';
    document.querySelectorAll('#setupStep2 .pet-option').forEach(o => o.classList.remove('selected'));
}

async function renderPetCarousel() {
    const container = $('petCarouselScroll');
    if (!container) return;
    if (carouselRendering) return;
    carouselRendering = true;
    container.innerHTML = '';
    try {
        const data = await api('GET', '/api/plants');
        if (!data.ok) return;

        data.plants.forEach(p => {
            const card = document.createElement('button');
            card.className = 'pet-carousel-card' + (p.id === data.active_slot ? ' active' : '');

            let imgHtml = '';
            if (p.plant_photo_url) {
                imgHtml = `<img class="pet-carousel-img" src="${p.plant_photo_url}" alt="${p.plant_name}" loading="lazy" />`;
            } else {
                imgHtml = `<div class="pet-carousel-placeholder">${petTypeIcon(p.pet_type)}</div>`;
            }

            card.innerHTML = `
                ${imgHtml}
                <div class="pet-carousel-info">
                    <span class="pet-carousel-pet-name">${p.pet_name}</span>
                    <span class="pet-carousel-plant-name">${p.plant_name}</span>
                    <span class="pet-carousel-slot-label">Slot ${p.id}</span>
                </div>
            `;
            card.addEventListener('click', async () => {
                if (p.id === data.active_slot) return;
                showLoading('Trocando planta...');
                try {
                    await api('GET', `/api/plants/${p.id}/switch`);
                    clearInterval(pollTimer);
                    clearInterval(petPollTimer);
                    dashInitialized = false;
                    await initDashboard();
                } finally {
                    hideLoading();
                }
            });
            container.appendChild(card);
        });

        // Add "+" button if under limit
        if (data.plants.length < 5) {
            const addBtn = document.createElement('button');
            addBtn.className = 'pet-carousel-add';
            addBtn.innerHTML = `<span class="pet-carousel-add-icon">+</span>`;
            addBtn.title = 'Adicionar planta';
            addBtn.addEventListener('click', async () => {
                try {
                    await api('POST', '/api/plants');
                    setupComplete = false;
                    dashInitialized = false;
                    resetSetupForm();
                    showPage('setup');
                } catch (err) {
                    alert(err.message);
                }
            });
            container.appendChild(addBtn);
        }
    } catch (e) { console.warn('Pet carousel load failed:', e); }
    finally { carouselRendering = false; }
}

// ==================== DASHBOARD ====================
let dashInitialized = false;
let pollTimer = null;
let petPollTimer = null;
let carouselRendering = false;

async function initDashboard() {
    if (dashInitialized) return;
    dashInitialized = true;

    // Load pet carousel
    await renderPetCarousel();

    // Load plant profile
    let hasPlant = false;
    try {
        const plant = await api('GET', '/api/plant-profile');
        if (plant.ok) {
            hasPlant = true;
            const p = plant.profile;
            $('dashPlantName').textContent = p.nome_popular || '—';
            $('dashPlantScientific').textContent = p.nome_cientifico || '';
            $('idealTemp').textContent = `Ideal: ${p.temperatura_ideal_min}–${p.temperatura_ideal_max}°C`;
            $('idealHum').textContent = `Ideal: ${p.umidade_ar_ideal_min}–${p.umidade_ar_ideal_max}%`;
            $('idealSoil').textContent = `Ideal: ${p.umidade_solo_ideal_min}–${p.umidade_solo_ideal_max}%`;

            // Populate modal
            $('modalPlantName').textContent = p.nome_popular || 'Desconhecida';
            $('modalPlantScientific').textContent = p.nome_cientifico || '';
            $('modalPlantDesc').textContent = p.descricao || '';
            $('modalIdealTemp').textContent = `${p.temperatura_ideal_min} a ${p.temperatura_ideal_max}°C`;
            $('modalIdealHum').textContent = `${p.umidade_ar_ideal_min} a ${p.umidade_ar_ideal_max}%`;
            $('modalIdealSoil').textContent = `${p.umidade_solo_ideal_min} a ${p.umidade_solo_ideal_max}%`;
            $('modalPlantCare').textContent = p.cuidados || '';
        }
    } catch (e) { console.warn('Plant profile load failed:', e); }

    // Show setup banner if no plant configured
    if (!hasPlant) {
        $('setupBanner').style.display = 'flex';
        $('petCarouselStrip').style.display = 'none';
        $('plantInfoBar').style.display = 'none';
    }
    $('setupBannerBtn').addEventListener('click', async () => {
        try {
            await api('POST', '/api/plants');
        } catch (e) { /* slot may already exist */ }
        setupComplete = false;
        dashInitialized = false;
        resetSetupForm();
        showPage('setup');
    });

    // Plant Modal Handlers
    $('plantInfoBar').addEventListener('click', () => {
        $('plantModal').style.display = 'flex';
    });
    $('closePlantModal').addEventListener('click', () => {
        $('plantModal').style.display = 'none';
    });
    $('plantModal').addEventListener('click', (e) => {
        if (e.target.id === 'plantModal') {
            $('plantModal').style.display = 'none';
        }
    });

    // Load pet config
    try {
        const pet = await api('GET', '/api/pet/config');
        if (pet.ok) {
            $('petNameDisplay').textContent = pet.config.name;
        }
    } catch (e) { console.warn('Pet config load failed:', e); }

    // Initial data load
    refreshCurrent();
    refreshPet();
    refreshCharts();

    // Polling
    pollTimer = setInterval(refreshCurrent, POLL_MS);
    petPollTimer = setInterval(refreshPet, PET_POLL_MS);
    setInterval(refreshCharts, 60000); // charts every 60s

    // Water button
    $('waterBtn').addEventListener('click', waterNow);
    checkWaterCooldown();

    // Menu & chart period handlers
    initMenu();
    initChartPeriod();
}

// ==================== REFRESH DATA ====================
function valClass(v, lo, hi) {
    return v >= lo && v <= hi ? 'val-ok' : v < lo ? 'val-warn' : 'val-bad';
}
function trendClass(t) {
    if (t === 'subindo') return 'trend-up';
    if (t === 'descendo') return 'trend-down';
    return 'trend-stable';
}
function trendText(t) {
    if (t === 'subindo') return '↑';
    if (t === 'descendo') return '↓';
    return '→';
}
function healthColor(s) {
    if (s >= 85) return '#4ade80';
    if (s >= 65) return '#22d3ee';
    if (s >= 45) return '#eab308';
    return '#f87171';
}

async function refreshCurrent() {
    try {
        const d = await api('GET', '/api/current');
        if (!d.ok) return;

        const ideal = d.ideal || { temp: [18, 28], humidity: [40, 70], soil: [15, 55] };

        // Temperature
        const tempEl = $('valTemp');
        tempEl.textContent = `${d.temperature}°C`;
        tempEl.className = 'dash-metric-value ' + valClass(d.temperature, ideal.temp[0], ideal.temp[1]);

        // Humidity
        const humEl = $('valHum');
        humEl.textContent = `${d.humidity}%`;
        humEl.className = 'dash-metric-value ' + valClass(d.humidity, ideal.humidity[0], ideal.humidity[1]);

        // Soil
        const soilEl = $('valSoil');
        soilEl.textContent = `${d.soil_smoothed}%`;
        soilEl.className = 'dash-metric-value ' + valClass(d.soil_smoothed, ideal.soil[0], ideal.soil[1]);

        // Health
        const healthEl = $('valHealth');
        healthEl.textContent = d.health.score;
        healthEl.style.color = healthColor(d.health.score);
        $('healthLabel').textContent = d.health.label;
        $('healthBar').style.width = d.health.score + '%';

        // Trends
        const tTemp = $('trendTemp');
        tTemp.className = 'dash-metric-trend ' + trendClass(d.trends.temperature);
        tTemp.textContent = trendText(d.trends.temperature);

        const tHum = $('trendHum');
        tHum.className = 'dash-metric-trend ' + trendClass(d.trends.humidity);
        tHum.textContent = trendText(d.trends.humidity);

        const tSoil = $('trendSoil');
        tSoil.className = 'dash-metric-trend ' + trendClass(d.trends.soil);
        tSoil.textContent = trendText(d.trends.soil);

        // Recommendation
        const rec = $('recommendation');
        rec.textContent = d.irrigation.message;
        rec.className = 'recommendation-card rec-' + d.irrigation.level;

        // Status
        $('statusDot').className = 'status-dot ' + (d.connected ? 'online' : 'offline');
        $('statusText').textContent = d.connected ? 'ESP32 conectado' : 'ESP32 desconectado';
        $('lastUpdate').textContent = d.timestamp ? `Última att: ${d.timestamp}` : '';

        // Show last pump time
        if (d.last_pump_at) {
            const pumpDate = new Date(d.last_pump_at);
            const pumpAgo = timeAgo(pumpDate);
            $('lastPumpRow').style.display = 'flex';
            $('lastPumpTime').textContent = pumpAgo;
        }

        // Fetch 24h min/max stats
        try {
            const stats = await api('GET', '/api/stats?hours=24');
            if (stats && stats.temperature) {
                const t = stats.temperature;
                const h = stats.humidity;
                const s = stats.soil_smoothed || stats.soil;
                if ($('minmaxTemp') && t) $('minmaxTemp').textContent = `↓${t.min?.toFixed(1) ?? '--'} ↑${t.max?.toFixed(1) ?? '--'}°C`;
                if ($('minmaxHum') && h) $('minmaxHum').textContent = `↓${h.min?.toFixed(0) ?? '--'} ↑${h.max?.toFixed(0) ?? '--'}%`;
                if ($('minmaxSoil') && s) $('minmaxSoil').textContent = `↓${s.min?.toFixed(0) ?? '--'} ↑${s.max?.toFixed(0) ?? '--'}%`;
            }
        } catch(e) { /* stats are optional */ }

    } catch (e) {
        console.error('Current fetch failed:', e);
    }
}

async function refreshPet() {
    try {
        const data = await api('GET', '/api/pet/current');
        if (!data.ok) return;

        const img = $('petImage');
        const placeholder = $('petPlaceholder');

        if (data.image) {
            img.src = 'data:image/png;base64,' + data.image;
            img.style.display = 'block';
            placeholder.style.display = 'none';
        }

        // Show speech bubble synced with image update
        const el = $('petSpeech');
        const textEl = $('petSpeechText');
        const phrase = (data.pet_phrases && data.pet_phrases.length) ? data.pet_phrases[0] : (data.pet_caption || data.event_of_day || '');
        if (phrase) {
            textEl.textContent = cleanText(phrase);
            el.style.display = 'block';
        } else {
            el.style.display = 'none';
        }
    } catch (e) {
        console.error('Pet fetch failed:', e);
    }
}

// ==================== SPARKLINE CHARTS ====================
function fmtTime(isoStr) {
    const d = new Date(isoStr);
    const h = d.getHours().toString().padStart(2, '0');
    const m = d.getMinutes().toString().padStart(2, '0');
    return `${h}:${m}`;
}

function drawSparkline(canvasId, data, color, idealRange, hoveredIndex = null, timestamps = null) {
    const canvas = $(canvasId);
    if (!canvas || !data || data.length === 0) return;

    const H_TOTAL = timestamps ? 116 : 100;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = H_TOTAL * dpr;
    canvas.style.height = H_TOTAL + 'px';

    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const W = rect.width;
    const H = 100;

    // Filter nulls
    const valid = data.filter(v => v !== null && v !== undefined);
    if (valid.length === 0) return;

    const min = Math.min(...valid) - 2;
    const max = Math.max(...valid) + 2;
    const range = max - min || 1;

    const toY = v => H - 8 - ((v - min) / range) * (H - 16);
    const toX = i => (i / (data.length - 1)) * W;

    // Ideal range band
    if (idealRange) {
        const y1 = toY(idealRange[1]);
        const y2 = toY(idealRange[0]);
        ctx.fillStyle = 'rgba(52, 199, 89, 0.08)';
        ctx.fillRect(0, y1, W, y2 - y1);
    }

    // Line
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    let started = false;
    for (let i = 0; i < data.length; i++) {
        if (data[i] === null || data[i] === undefined) continue;
        const x = toX(i);
        const y = toY(data[i]);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
    }
    ctx.stroke();

    // Fill under
    if (started) {
        ctx.lineTo(toX(data.length - 1), H);
        ctx.lineTo(toX(0), H);
        ctx.closePath();
        ctx.fillStyle = color.replace('1)', '0.06)');
        ctx.fill();
    }

    // Last value label
    const lastVal = valid[valid.length - 1];
    ctx.fillStyle = color;
    ctx.font = 'bold 11px Inter, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(lastVal.toFixed(1), W - 4, toY(lastVal) - 6);

    // Min/Max labels
    ctx.fillStyle = '#aeaeb2';
    ctx.font = '9px Inter, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(Math.min(...valid).toFixed(0), 2, H - 2);
    ctx.textAlign = 'right';
    ctx.fillText(Math.max(...valid).toFixed(0), W - 2, 10);

    // Time axis labels
    if (timestamps && timestamps.length > 0) {
        ctx.fillStyle = '#aeaeb2';
        ctx.font = '8px Inter, sans-serif';
        const ty = H + 11;
        ctx.textAlign = 'left';
        ctx.fillText(fmtTime(timestamps[0]), 2, ty);
        const midIdx = Math.floor((timestamps.length - 1) / 2);
        ctx.textAlign = 'center';
        ctx.fillText(fmtTime(timestamps[midIdx]), W / 2, ty);
        ctx.textAlign = 'right';
        ctx.fillText(fmtTime(timestamps[timestamps.length - 1]), W - 2, ty);
    }

    // Hover highlight
    if (hoveredIndex !== null && data[hoveredIndex] !== null && data[hoveredIndex] !== undefined) {
        const hx = toX(hoveredIndex);
        const hv = data[hoveredIndex];
        const hy = toY(hv);
        const px = 5, py = 3, labelH = 14;

        // Circle
        ctx.beginPath();
        ctx.arc(hx, hy, 5, 0, Math.PI * 2);
        ctx.fillStyle = '#fff';
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2.5;
        ctx.stroke();

        // Label
        const label = hv.toFixed(1);
        ctx.font = 'bold 10px Inter, sans-serif';
        const tw = ctx.measureText(label).width;
        const lx = Math.min(Math.max(hx - tw / 2 - px, 1), W - tw - px * 2 - 1);
        const ly = Math.max(hy - labelH - 10, 2);

        ctx.fillStyle = 'rgba(30, 20, 10, 0.82)';
        ctx.fillRect(lx, ly, tw + px * 2, labelH + py * 2);

        ctx.fillStyle = '#fff';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        ctx.fillText(label, lx + px, ly + py + 1);
        ctx.textBaseline = 'alphabetic';
    }
}

async function refreshCharts() {
    try {
        const data = await api('GET', `/api/history?hours=${chartHours}`);
        if (!data || !data.count) return;

        const ts = data.timestamps || null;
        drawSparkline('chartTemp', data.temperature, 'rgba(255, 149, 0, 1)', null, null, ts);
        drawSparkline('chartHum', data.humidity, 'rgba(0, 122, 255, 1)', null, null, ts);
        drawSparkline('chartSoil', data.soil_smoothed, 'rgba(52, 199, 89, 1)', null, null, ts);

        setupChartHover('chartTemp', data.temperature, 'rgba(255, 149, 0, 1)', null, ts);
        setupChartHover('chartHum', data.humidity, 'rgba(0, 122, 255, 1)', null, ts);
        setupChartHover('chartSoil', data.soil_smoothed, 'rgba(52, 199, 89, 1)', null, ts);
    } catch (e) {
        console.warn('Chart load failed:', e);
    }
}

function initChartPeriod() {
    const sel = $('chartPeriodSelect');
    if (!sel) return;
    sel.addEventListener('change', () => {
        chartHours = parseInt(sel.value);
        refreshCharts();
    });
}

function setupChartHover(canvasId, data, color, idealRange, timestamps = null) {
    const canvas = $(canvasId);
    if (!canvas || !data || data.length === 0) return;

    // Abort previous listeners for this canvas
    if (chartAbortControllers[canvasId]) {
        chartAbortControllers[canvasId].abort();
    }
    const controller = new AbortController();
    chartAbortControllers[canvasId] = controller;
    const { signal } = controller;

    canvas.addEventListener('mousemove', (e) => {
        const rect = canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const idx = Math.round((x / rect.width) * (data.length - 1));
        const clamped = Math.max(0, Math.min(idx, data.length - 1));
        drawSparkline(canvasId, data, color, idealRange, clamped, timestamps);
    }, { signal });

    canvas.addEventListener('mouseleave', () => {
        drawSparkline(canvasId, data, color, idealRange, null, timestamps);
    }, { signal });
}

// ==================== MENU ====================
function initMenu() {
    const menu = $('dashMenu');

    $('menuBtn').addEventListener('click', (e) => {
        e.stopPropagation();
        menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
    });
    document.addEventListener('click', () => { menu.style.display = 'none'; });

    $('menuLogout').addEventListener('click', () => {
        token = '';
        localStorage.removeItem('hoya_token');
        clearInterval(pollTimer);
        clearInterval(petPollTimer);
        dashInitialized = false;
        showPage('login');
    });

    // Edit Pet modal
    let editPetType = '';
    $('menuEditPet').addEventListener('click', async () => {
        menu.style.display = 'none';
        // Load current pet config
        try {
            const pet = await api('GET', '/api/pet/config');
            if (pet.ok) {
                $('editPetNameInput').value = pet.config.name || '';
                editPetType = pet.config.type || 'cat';
                document.querySelectorAll('#editPetSelector .pet-option').forEach(o => {
                    o.classList.toggle('selected', o.dataset.type === editPetType);
                });
            }
        } catch (e) { /* ignore */ }
        $('editPetModal').style.display = 'flex';
    });

    $('closeEditPetModal').addEventListener('click', () => {
        $('editPetModal').style.display = 'none';
    });
    $('editPetModal').addEventListener('click', (e) => {
        if (e.target.id === 'editPetModal') $('editPetModal').style.display = 'none';
    });

    document.querySelectorAll('#editPetSelector .pet-option').forEach(opt => {
        opt.addEventListener('click', () => {
            document.querySelectorAll('#editPetSelector .pet-option').forEach(o => o.classList.remove('selected'));
            opt.classList.add('selected');
            editPetType = opt.dataset.type;
        });
    });

    $('saveEditPetBtn').addEventListener('click', async () => {
        const name = $('editPetNameInput').value.trim();
        if (!name || !editPetType) {
            alert('Preencha o nome e escolha o tipo do pet.');
            return;
        }
        const btn = $('saveEditPetBtn');
        btnLoading(btn, true);
        try {
            await api('POST', '/api/pet/configure', { name, type: editPetType });
            $('petNameDisplay').textContent = name;
            $('editPetModal').style.display = 'none';
            await renderPetCarousel();
        } catch (e) {
            alert('Erro: ' + e.message);
        } finally {
            btnLoading(btn, false);
        }
    });

    $('menuCalibrateSoil').addEventListener('click', async () => {
        menu.style.display = 'none';
        await initCalibrate();
        $('calibrateModal').style.display = 'flex';
    });

    $('menuGeneratePet').addEventListener('click', async () => {
        menu.style.display = 'none';
        showLoading('Gerando nova imagem...');
        try {
            await api('POST', '/api/pet/generate');
            await refreshPet();
        } catch (e) {
            alert('Erro: ' + e.message);
        } finally {
            hideLoading();
        }
    });

    $('menuDeletePlant').addEventListener('click', async () => {
        menu.style.display = 'none';
        try {
            const data = await api('GET', '/api/plants');
            if (data.plants.length <= 1) {
                alert('Você precisa ter pelo menos 1 planta.');
                return;
            }
            const active = data.plants.find(p => p.id === data.active_slot);
            if (active) {
                deletePlant(active.id, active.plant_name);
            }
        } catch (e) {
            alert('Erro: ' + e.message);
        }
    });

    const updatePhotoInput = $('updatePhotoInput');
    $('menuUpdatePhoto').addEventListener('click', () => {
        menu.style.display = 'none';
        updatePhotoInput.click();
    });
    updatePhotoInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        showLoading('Atualizando foto...');
        try {
            const formData = new FormData();
            formData.append('file', file);
            const data = await api('POST', '/api/setup/plant-photo', formData, true);
            if (data.ok) {
                const p = data.profile;
                $('dashPlantName').textContent = p.nome_popular || '—';
                $('dashPlantScientific').textContent = p.nome_cientifico || '';
                $('idealTemp').textContent = `Ideal: ${p.temperatura_ideal_min}–${p.temperatura_ideal_max}°C`;
                $('idealHum').textContent = `Ideal: ${p.umidade_ar_ideal_min}–${p.umidade_ar_ideal_max}%`;
                $('idealSoil').textContent = `Ideal: ${p.umidade_solo_ideal_min}–${p.umidade_solo_ideal_max}%`;
            }
        } catch (e) {
            alert('Erro: ' + e.message);
        } finally {
            hideLoading();
            updatePhotoInput.value = '';
        }
    });

    // Update pet reference photo from dashboard
    const updatePetPhotoInput = $('updatePetPhotoInput');
    $('menuUpdatePetPhoto').addEventListener('click', () => {
        menu.style.display = 'none';
        updatePetPhotoInput.click();
    });
    updatePetPhotoInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        showLoading('Enviando foto do pet...');
        try {
            const petFormData = new FormData();
            petFormData.append('file', file);
            await api('POST', '/api/pet/upload-photo', petFormData, true);
            showLoading('Regenerando pet com nova foto...');
            await api('POST', '/api/pet/generate');
            await refreshPet();
        } catch (e) {
            alert('Erro: ' + e.message);
        } finally {
            hideLoading();
            updatePetPhotoInput.value = '';
        }
    });
}

// ==================== CALIBRACAO DE SOLO ====================
let calCountdownTimer = null;

async function initCalibrate() {
    const modal = $('calibrateModal');

    // Carrega calibracao atual
    try {
        const data = await api('GET', '/api/calibrate/soil');
        if (data.calibration && data.calibration.soaked_adc) {
            const c = data.calibration;
            const date = c.calibrated_at ? new Date(c.calibrated_at).toLocaleDateString('pt-BR') : '—';
            $('calCurrentInfo').textContent = `Calibracao atual: ADC encharcado = ${c.soaked_adc} (${date})`;
        } else {
            $('calCurrentInfo').textContent = 'Sem calibracao salva. Usando valores padrao.';
        }
    } catch (e) { /* ignore */ }

    // Resetar estado
    $('calStep0').style.display = 'block';
    $('calStep1').style.display = 'none';
    $('calStep2').style.display = 'none';
    $('calResult').style.display = 'none';

    $('closeCalibrateModal').onclick = () => {
        clearInterval(calCountdownTimer);
        modal.style.display = 'none';
    };
    modal.addEventListener('click', (e) => {
        if (e.target.id === 'calibrateModal') {
            clearInterval(calCountdownTimer);
            modal.style.display = 'none';
        }
    });

    // Passo 1: acionar bomba 5s
    const startBtn = $('calStartBtn');
    startBtn.onclick = async () => {
        const text = startBtn.querySelector('.btn-text');
        const loader = startBtn.querySelector('.btn-loader');
        startBtn.disabled = true;
        text.style.display = 'none';
        loader.style.display = 'inline';
        try {
            await api('POST', '/api/calibrate/soil');
        } catch (e) {
            alert('Erro: ' + e.message);
            startBtn.disabled = false;
            text.style.display = 'inline';
            loader.style.display = 'none';
            return;
        }
        // Passa para countdown
        $('calStep0').style.display = 'none';
        $('calStep1').style.display = 'block';
        const WAIT_S = 30;
        let remaining = WAIT_S;
        $('calCountdown').textContent = remaining;
        $('calProgressBar').style.width = '100%';

        calCountdownTimer = setInterval(() => {
            remaining--;
            $('calCountdown').textContent = remaining;
            $('calProgressBar').style.width = ((remaining / WAIT_S) * 100) + '%';
            if (remaining <= 0) {
                clearInterval(calCountdownTimer);
                $('calStep1').style.display = 'none';
                $('calStep2').style.display = 'block';
            }
        }, 1000);
    };

    // Passo 2: salvar calibracao
    const saveBtn = $('calSaveBtn');
    saveBtn.onclick = async () => {
        const text = saveBtn.querySelector('.btn-text');
        const loader = saveBtn.querySelector('.btn-loader');
        saveBtn.disabled = true;
        text.style.display = 'none';
        loader.style.display = 'inline';
        try {
            const data = await api('POST', '/api/calibrate/soil/save');
            $('calStep2').style.display = 'none';
            $('calResult').style.display = 'block';
            $('calResult').textContent = `Calibrado! ADC encharcado: ${data.soaked_adc} | ADC seco: ${data.dry_adc}. A porcentagem do solo sera recalculada automaticamente.`;
        } catch (e) {
            alert('Erro ao salvar: ' + e.message);
            saveBtn.disabled = false;
            text.style.display = 'inline';
            loader.style.display = 'none';
        }
    };
}

// ==================== REGAR AGORA ====================
async function waterNow() {
    const btn = $('waterBtn');
    const text = btn.querySelector('.btn-text');
    const loader = btn.querySelector('.btn-loader');

    btn.disabled = true;
    btn.classList.add('watering');
    text.style.display = 'none';
    loader.style.display = 'inline';

    try {
        await api('POST', '/api/water?seconds=5');
        // Comando enfileirado — ESP32 executa no proximo ciclo (~3s)
        // Salva cooldown de 5 min no localStorage
        const cooldownUntil = Date.now() + 5 * 60 * 1000;
        localStorage.setItem('waterCooldownUntil', cooldownUntil.toString());
        // Aguarda e atualiza dados
        await new Promise(r => setTimeout(r, 4000));
        await refreshCurrent();
    } catch (e) {
        alert('Erro: ' + (e.message || 'Falha ao enviar comando'));
    } finally {
        btn.disabled = false;
        btn.classList.remove('watering');
        text.style.display = 'inline';
        loader.style.display = 'none';
        checkWaterCooldown();
    }
}

function checkWaterCooldown() {
    const btn = $('waterBtn');
    if (!btn) return;
    const cooldownUntil = parseInt(localStorage.getItem('waterCooldownUntil') || '0');
    const remaining = Math.ceil((cooldownUntil - Date.now()) / 1000);
    if (remaining > 0) {
        const mins = Math.ceil(remaining / 60);
        const text = btn.querySelector('.btn-text');
        if (text) text.textContent = `Aguardar ${mins}min`;
        btn.disabled = true;
        btn.classList.add('water-btn-cooldown');
        // Re-check every 30s
        setTimeout(checkWaterCooldown, 30000);
    } else {
        const text = btn.querySelector('.btn-text');
        if (text) text.textContent = 'REGAR AGORA';
        btn.disabled = false;
        btn.classList.remove('water-btn-cooldown');
        localStorage.removeItem('waterCooldownUntil');
    }
}

// ==================== LANDING PAGE ====================
let landingObserverInit = false;

function initNavScroll() {
    const nav = document.getElementById('lpNav');
    const landing = document.getElementById('pageLanding');
    if (!nav || !landing) return;
    landing.addEventListener('scroll', () => {
        nav.classList.toggle('lp-nav--scrolled', landing.scrollTop > 40);
    }, { passive: true });
}

function initLanding() {
    $('landingLoginBtn')?.addEventListener('click', () => showPage('login'));
    $('landingCtaTop')?.addEventListener('click', () => showPage('login'));
    $('landingCtaBottom')?.addEventListener('click', () => showPage('signup'));
    document.querySelectorAll('.back-to-landing').forEach(el =>
        el.addEventListener('click', e => { e.preventDefault(); showPage('landing'); })
    );
    initCarousel();
    initNavScroll();
}

function initLandingObserver() {
    if (landingObserverInit) return;
    landingObserverInit = true;
    const root = $('pageLanding');
    const obs = new IntersectionObserver(entries => {
        entries.forEach(e => {
            if (e.isIntersecting) { e.target.classList.add('revealed'); obs.unobserve(e.target); }
        });
    }, { root, threshold: 0.1, rootMargin: '0px 0px -80px 0px' });
    document.querySelectorAll('.lp-reveal').forEach(el => obs.observe(el));
}

function initCarousel() {
    const track = $('carouselTrack');
    if (!track) return;
    const slides = track.querySelectorAll('.lp-phone-slide');
    const dots = $('carouselDots')?.querySelectorAll('.lp-dot') || [];
    let cur = 0;
    function go(i) {
        if (i < 0) i = slides.length - 1;
        if (i >= slides.length) i = 0;
        slides.forEach(s => s.classList.remove('active'));
        dots.forEach(d => d.classList.remove('active'));
        slides[i].classList.add('active');
        dots[i]?.classList.add('active');
        cur = i;
    }
    dots.forEach((d, i) => d.addEventListener('click', () => go(i)));
    setInterval(() => { if ($('pageLanding').style.display !== 'none') go(cur + 1); }, 6000);
}

// ==================== INIT ====================
async function checkSetupAndRoute() {
    try {
        await api('GET', '/api/auth/check');
        showPage('dash');
        initDashboard();
    } catch (e) {
        showPage('login');
    }
}

async function init() {
    initAuth();
    initSetup();
    initLanding();

    if (token) {
        try {
            await api('GET', '/api/auth/check');
            await checkSetupAndRoute();
        } catch (e) {
            showPage('landing');
        }
    } else {
        showPage('landing');
    }
}

document.addEventListener('DOMContentLoaded', init);
