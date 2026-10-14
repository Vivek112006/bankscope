'use strict';
const $ = s => document.querySelector(s);
const CSRF = $('meta[name=csrf]').content;
const USER_RE = /^[A-Za-z0-9_.-]{3,30}$/;
const COMMON = ['password', 'password1', '12345678', '123456789', 'qwertyui', 'qwerty123', 'iloveyou', 'admin123', 'letmein1', 'welcome1', 'abc12345', '11111111'];
let mode = 'login';

function store(op, v) { try { return op === 'get' ? localStorage.getItem('bankscope_user') : op === 'set' ? localStorage.setItem('bankscope_user', v) : localStorage.removeItem('bankscope_user'); } catch (e) { return null; } }

function fieldError(id, msg) {
  $('#err-' + id).textContent = msg || '';
  const input = $('#' + id);
  input.setAttribute('aria-invalid', msg ? 'true' : 'false');
  input.closest('.input').classList.toggle('bad', !!msg);
}
function clearErrors() { ['username', 'password', 'confirm'].forEach(i => fieldError(i, '')); showError(''); }
function showError(msg) { const e = $('#auth-error'); e.textContent = msg; e.hidden = !msg; }

function setMode(m) {
  mode = m;
  const reg = m === 'register';
  $('#tab-login').classList.toggle('on', !reg); $('#tab-register').classList.toggle('on', reg);
  $('#tab-login').setAttribute('aria-selected', String(!reg)); $('#tab-register').setAttribute('aria-selected', String(reg));
  $('#auth-title').textContent = reg ? 'Create your account' : 'Welcome back';
  $('#auth-sub').textContent = reg ? 'Register to start analysing your datasets.' : 'Sign in to open your dashboard.';
  $('#submit-text').textContent = reg ? 'Create account' : 'Sign in';
  $('#auth-submit').textContent = '';          // rebuilt below
  $('#auth-submit').innerHTML = '<span class="spin" hidden></span><span id="submit-text">' + (reg ? 'Create account' : 'Sign in') + '</span>';
  $('#confirm-wrap').hidden = !reg; $('#rules').hidden = !reg; $('#strength').hidden = !reg;
  $('#forgot-btn').hidden = reg; $('#forgot').hidden = true;
  $('#password').autocomplete = reg ? 'new-password' : 'current-password';
  $('#password').placeholder = reg ? 'Create a password' : 'Your password';
  $('#switch-line').innerHTML = reg ? 'Already have an account? <button type="button" class="link" id="switch-btn">Sign in</button>'
    : 'New to BankScope? <button type="button" class="link" id="switch-btn">Create an account</button>';
  $('#switch-btn').onclick = () => setMode(reg ? 'login' : 'register');
  clearErrors();
  if (reg) updateStrength();
  history.replaceState(null, '', reg ? '#register' : location.pathname);
}

function strength(p) {
  if (!p) return { score: 0, label: '' };
  let s = 0;
  if (p.length >= 8) s++;
  if (p.length >= 12) s++;
  if (/[a-z]/.test(p) && /[A-Z]/.test(p)) s++;
  if (/\d/.test(p)) s++;
  if (/[^A-Za-z0-9]/.test(p)) s++;
  if (COMMON.includes(p.toLowerCase()) || /^(.)\1+$/.test(p) || /^\d+$/.test(p)) s = Math.min(s, 1);
  if (p.length < 8) s = Math.min(s, 1);
  return { score: s, label: ['Too weak', 'Weak', 'Fair', 'Good', 'Strong', 'Excellent'][s] };
}
function updateStrength() {
  const { score, label } = strength($('#password').value);
  const bar = $('#meter-bar');
  bar.style.width = ($('#password').value ? Math.max(8, score * 20) : 0) + '%';
  bar.className = 's' + score;
  $('#strength-text').textContent = label ? 'Password strength: ' + label : 'Use 8+ characters; mix letters, numbers and symbols.';
}

function validate() {
  clearErrors();
  const u = $('#username').value.trim(), p = $('#password').value;
  let ok = true;
  if (!u) { fieldError('username', 'Enter your username.'); ok = false; }
  else if (mode === 'register' && !USER_RE.test(u)) { fieldError('username', '3–30 characters: letters, numbers, dot, dash or underscore.'); ok = false; }
  if (!p) { fieldError('password', 'Enter your password.'); ok = false; }
  else if (mode === 'register') {
    if (p.length < 8) { fieldError('password', 'Use at least 8 characters.'); ok = false; }
    else if (strength(p).score <= 1) { fieldError('password', 'That password is too easy to guess.'); ok = false; }
    else if (p !== $('#confirm').value) { fieldError('confirm', 'Passwords do not match.'); ok = false; }
  }
  return ok;
}

function busy(on) {
  const b = $('#auth-submit');
  b.disabled = on;
  const sp = b.querySelector('.spin'); if (sp) sp.hidden = !on;
  ['username', 'password', 'confirm'].forEach(i => { $('#' + i).disabled = on; });
}

async function submit(ev) {
  ev.preventDefault();
  if (!validate()) { const bad = document.querySelector('.input.bad input'); if (bad) bad.focus(); return; }
  const username = $('#username').value.trim(), password = $('#password').value;
  busy(true);
  try {
    const res = await fetch('/api/auth/' + mode, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF }, body: JSON.stringify({ username, password }) });
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok || !data || !data.success) {
      const msg = (data && data.error) || `Request failed (${res.status}).`;
      if (res.status === 409) fieldError('username', msg); else showError(msg);
      busy(false);
      if (mode === 'login') { $('#password').value = ''; $('#password').focus(); }
      return;
    }
    if ($('#remember').checked) store('set', username.toLowerCase()); else store('del');
    $('#auth-submit').querySelector('#submit-text').textContent = 'Signing you in…';
    window.location.href = '/';
  } catch (e) {
    showError('Cannot reach the server. Check that BankScope is still running.');
    busy(false);
  }
}

function init() {
  $('#tab-login').onclick = () => setMode('login');
  $('#tab-register').onclick = () => setMode('register');
  $('#switch-btn').onclick = () => setMode('register');
  $('#auth-form').addEventListener('submit', submit);
  $('#forgot-btn').onclick = () => { $('#forgot').hidden = !$('#forgot').hidden; };
  $('#toggle-pw').onclick = () => {
    const show = $('#password').type === 'password';
    $('#password').type = show ? 'text' : 'password';
    if ($('#confirm')) $('#confirm').type = show ? 'text' : 'password';
    $('#toggle-pw').setAttribute('aria-pressed', String(show));
    $('#toggle-pw').setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    $('#toggle-pw').classList.toggle('on', show);
  };
  ['username', 'password', 'confirm'].forEach(i => $('#' + i).addEventListener('input', () => { fieldError(i, ''); showError(''); if (i === 'password' && mode === 'register') updateStrength(); }));
  const caps = e => { if (e.getModifierState) $('#caps').hidden = !e.getModifierState('CapsLock'); };
  ['password', 'confirm'].forEach(i => { $('#' + i).addEventListener('keydown', caps); $('#' + i).addEventListener('keyup', caps); $('#' + i).addEventListener('blur', () => { $('#caps').hidden = true; }); });
  const saved = store('get');
  if (saved) { $('#username').value = saved; $('#remember').checked = true; $('#password').focus(); }
  if (location.hash === '#register') setMode('register');
}
init();
