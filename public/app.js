/*
 * StudyBuddy dashboard chat.
 *
 * Talks to the same FastAPI that serves the Telegram webhook and the cron tick:
 *   GET  /api/status          — auth gate + everything the dashboard renders
 *   POST /api/login           — password → HttpOnly session cookie
 *   POST /api/chat            — one question → grounded answer + source links
 *   GET  /api/digests/latest  — today's digest for the toast card
 *   POST /api/sync            — manual Canvas refresh
 *
 * No tokens, no analytics, no third-party calls. Everything the page shows comes
 * from those five endpoints.
 */
(function () {
  'use strict';

  var el = function (id) { return document.getElementById(id); };
  var chatlog = el('chatlog');
  var askform = el('askform');
  var askinput = el('askinput');
  var asksend = el('asksend');
  var authform = el('authform');
  var authinput = el('authinput');
  var authnote = el('authnote');
  var statusline = el('statusline');
  var botmount = el('postbot');

  var mascot = window.Postbot.create({
    directions: '/mascots/postbot-directions.webp',
    reactions: '/mascots/postbot-reactions.webp',
    size: 150,
    label: 'Postbot',
  });
  botmount.appendChild(mascot.el);

  var busy = false;

  // ------------------------------------------------------------------ helpers

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  // A very small markdown subset: **bold** and [text](url). The chat engine is
  // told to keep answers to these two, and escaping happens first so nothing
  // the model or the data produces can become markup.
  function renderMarkdown(text) {
    var html = escapeHtml(text);
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (
      match, label, url
    ) {
      return '<a href="' + url + '" target="_blank" rel="noopener">' + label + '</a>';
    });
    return html.replace(/\n/g, '<br>');
  }

  function addMessage(role, html, sources) {
    var wrap = document.createElement('div');
    wrap.className = 'msg msg-' + role;
    var body = document.createElement('div');
    body.className = 'msg-body';
    body.innerHTML = html;
    wrap.appendChild(body);
    if (sources && sources.length) {
      var src = document.createElement('div');
      src.className = 'msg-sources';
      sources.forEach(function (s, i) {
        if (!s.url) return;
        var a = document.createElement('a');
        a.href = s.url;
        a.target = '_blank';
        a.rel = 'noopener';
        var label = (s.course ? s.course + ' · ' : '') + s.title;
        a.textContent = label.length > 42 ? label.slice(0, 40) + '…' : label;
        if (i > 0) src.appendChild(document.createTextNode(' '));
        src.appendChild(a);
      });
      if (src.childNodes.length) wrap.appendChild(src);
    }
    chatlog.appendChild(wrap);
    chatlog.scrollTop = chatlog.scrollHeight;
    return wrap;
  }

  function react(name, ms) {
    if (mascot) mascot.react(name, ms);
  }

  function setBusy(value) {
    busy = value;
    asksend.disabled = value;
    askinput.disabled = value;
    askform.classList.toggle('is-busy', value);
  }

  function showAuth(note) {
    authform.hidden = false;
    authnote.textContent = note || '';
    askform.hidden = true;
    authinput.focus();
  }

  function showAsk() {
    authform.hidden = true;
    askform.hidden = false;
    askinput.focus();
  }

  // ------------------------------------------------------------------- boot

  function greet() {
    addMessage(
      'bot',
      "Hey, I'm your StudyBuddy. I keep an eye on Canvas so you don't have to. " +
        'Ask me about assignments, deadlines, announcements, or what changed today.'
    );
  }

  async function init() {
    try {
      var response = await fetch('/api/status', { credentials: 'same-origin' });
      if (response.status === 401) {
        showAuth('Sign in with your dashboard password to start asking.');
        return;
      }
      if (response.status === 503) {
        showAuth('');
        authnote.textContent =
          'The dashboard is not enabled on this deployment yet. Set DASHBOARD_PASSWORD ' +
          'and APP_SECRET, then reload.';
        return;
      }
      if (!response.ok) {
        addMessage('bot', "I couldn't reach the StudyBuddy server. Check your connection and reload.");
        return;
      }
      var status = await response.json();
      showAsk();
      renderStatus(status);
      loadDigest(status);
    } catch (error) {
      addMessage('bot', "Can't reach the StudyBuddy server right now. Try again in a moment.");
    }
  }

  function renderStatus(status) {
    var parts = [];
    if (status.canvas && status.canvas.last_sync_error) {
      parts.push('Canvas connection needs attention');
      addMessage(
        'bot',
        "StudyBuddy lost access to Canvas.\n\nReconnect your Canvas account and I'll continue " +
          'syncing your courses.'
      );
    }
    var counts = status.counts || {};
    var dueToday = counts.due_today || 0;
    var overdue = counts.overdue || 0;
    var dueWeek = counts.due_week || 0;
    parts.unshift(
      dueWeek + ' due this week · ' + dueToday + ' today' + (overdue ? ' · ' + overdue + ' overdue' : '')
    );
    var line = document.createElement('span');
    line.textContent = parts.join(' — ');
    statusline.innerHTML = '';
    statusline.appendChild(line);

    if (status.canvas && status.canvas.last_sync_at) {
      var synced = document.createElement('span');
      synced.className = 'syncinfo';
      synced.textContent = 'Last synced ' + timeAgo(status.canvas.last_sync_at);
      statusline.appendChild(synced);
    }
    var refresh = document.createElement('button');
    refresh.type = 'button';
    refresh.className = 'refreshbtn';
    refresh.textContent = 'Refresh Canvas';
    refresh.setAttribute('aria-label', 'Refresh Canvas data now');
    refresh.addEventListener('click', manualSync);
    statusline.appendChild(refresh);
  }

  function timeAgo(iso) {
    var then = new Date(iso).getTime();
    var minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
    if (minutes < 1) return 'just now';
    if (minutes === 1) return '1 minute ago';
    if (minutes < 60) return minutes + ' minutes ago';
    var hours = Math.round(minutes / 60);
    return hours === 1 ? '1 hour ago' : hours + ' hours ago';
  }

  async function loadDigest(status) {
    try {
      var response = await fetch('/api/digests/latest', { credentials: 'same-origin' });
      if (!response.ok) return;
      var digest = await response.json();
      var title = el('toast-title');
      var body = el('toast-body');
      var time = el('toast-time');
      if (!digest.fresh) {
        title.textContent = 'Digest sent!';
        time.textContent = formatTime(new Date(digest.sent_at));
      }
      var counts = (status && status.counts) || {};
      var parts = [];
      if (counts.due_week) parts.push(counts.due_week + ' due this week');
      if (counts.overdue) parts.push(counts.overdue + ' overdue');
      if (counts.announcements_week) parts.push(counts.announcements_week + ' new posts');
      // Fall back to the digest's own first bullet when counts are flat.
      if (!parts.length) {
        var firstBullet = String(digest.body || '')
          .split('\n')
          .map(function (line) { return line.replace(/\*\*/g, '').trim(); })
          .find(function (line) { return line.indexOf('- ') === 0; });
        body.textContent = firstBullet
          ? firstBullet.slice(2)
          : "You're clear for now.";
        return;
      }
      body.textContent = parts.join(' · ');
    } catch (error) {
      /* The toast keeps its decorative copy when the digest can't load. */
    }
  }

  function formatTime(date) {
    var hours = date.getHours() % 12 || 12;
    var minutes = String(date.getMinutes()).padStart(2, '0');
    var suffix = date.getHours() < 12 ? 'AM' : 'PM';
    return hours + ':' + minutes + ' ' + suffix;
  }

  async function manualSync() {
    react('blink');
    try {
      var response = await fetch('/api/sync', {
        method: 'POST',
        credentials: 'same-origin',
      });
      if (response.status === 401) {
        showAuth('Your session expired. Sign in again.');
        return;
      }
      var result = await response.json();
      if (!result.ok) {
        addMessage('bot', "The sync didn't go through: " + (result.error || 'unknown problem') + '.');
        react('dizzy', 900);
        return;
      }
      react('delighted', 900);
      addMessage('bot', 'Synced. ' + (result.summary || '').split('\n').slice(0, 2).join(', ') + '.');
      init();
    } catch (error) {
      addMessage('bot', "Can't reach the StudyBuddy server right now.");
      react('dizzy', 900);
    }
  }

  // ------------------------------------------------------------------- send

  async function send(text) {
    if (busy || !text.trim()) return;
    setBusy(true);
    addMessage('user', renderMarkdown(text));
    askinput.value = '';
    react('blink');
    var thinking = addMessage('bot', 'Checking Canvas…');
    thinking.classList.add('msg-thinking');

    try {
      var response = await fetch('/api/chat', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      thinking.remove();

      if (response.status === 401) {
        showAuth('Your session expired. Sign in again.');
        return;
      }
      if (!response.ok) {
        var detail = '';
        try {
          detail = (await response.json()).detail || '';
        } catch (error) { /* non-JSON error body */ }
        addMessage('bot', detail || "Something went wrong on the server. Try again in a moment.");
        react('dizzy', 900);
        return;
      }
      var answer = await response.json();
      addMessage('bot', renderMarkdown(answer.answer), answer.sources);
      react('delighted', 900);
    } catch (error) {
      thinking.remove();
      addMessage(
        'bot',
        "Can't reach the StudyBuddy server right now. Check your connection and try again."
      );
      react('dizzy', 900);
    } finally {
      setBusy(false);
      askinput.focus();
    }
  }

  // ------------------------------------------------------------------ wiring

  askform.addEventListener('submit', function (event) {
    event.preventDefault();
    send(askinput.value);
  });
  // Enter sends, Shift+Enter folds a new line; the box grows with its content.
  askinput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send(askinput.value);
    }
  });
  askinput.addEventListener('input', function () {
    askinput.style.height = 'auto';
    askinput.style.height = Math.min(askinput.scrollHeight, 64) + 'px';
  });
  document.querySelectorAll('.pchip').forEach(function (chip) {
    chip.addEventListener('click', function () {
      askinput.value = chip.getAttribute('data-q') || chip.textContent;
      send(askinput.value);
    });
  });
  authform.addEventListener('submit', async function (event) {
    event.preventDefault();
    authnote.textContent = '';
    try {
      var response = await fetch('/api/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: authinput.value }),
      });
      if (response.ok) {
        authinput.value = '';
        chatlog.innerHTML = '';
        greet();
        init();
        return;
      }
      var body = await response.json().catch(function () { return {}; });
      authnote.textContent = body.detail || "That didn't work. Try again.";
    } catch (error) {
      authnote.textContent = "Can't reach the StudyBuddy server right now.";
    }
  });

  greet();
  init();
})();
