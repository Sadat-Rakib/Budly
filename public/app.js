/*
 * Budly dashboard chat.
 *
 * The dashboard talks to the local Budly process (default http://127.0.0.1:8000):
 *   GET  /api/status          — configuration state, sync info, workload counts
 *   POST /api/canvas/test     — setup flow's "Test connection"
 *   POST /api/sync            — manual "Refresh Canvas"
 *   POST /api/chat            — one question → grounded answer + source links
 *   GET  /api/digests/latest  — today's digest for the toast card
 *
 * The same page also serves as the project showcase, where no Budly process is
 * behind it. There the page runs in demo mode: clearly labelled, it answers
 * with canned content and points people to the download. It never pretends to
 * see a real Canvas account.
 *
 * No tokens, no analytics, no third-party calls.
 */
(function () {
  'use strict';

  var el = function (id) { return document.getElementById(id); };
  var chatlog = el('chatlog');
  var askform = el('askform');
  var askinput = el('askinput');
  var asksend = el('asksend');
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
  var demoMode = false;

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

  // ------------------------------------------------------------------- boot

  function greet() {
    addMessage(
      'bot',
      "Hey, I'm Budly. Ask me anything about your Canvas — assignments, deadlines, " +
        'announcements, or what changed today.'
    );
  }

  async function init() {
    try {
      var response = await fetch('/api/status');
      if (!response.ok) throw new Error('not a Budly backend');
      var status = await response.json();
      renderStatus(status);
      loadDigest();
      if (!status.canvas.configured && !status.canvas.mock) {
        addMessage(
          'bot',
          'Budly is running, but Canvas is not connected yet.\n\nAdd your Canvas URL and ' +
            'access token to .env, restart Budly, then hit "Refresh Canvas".'
        );
      }
      return;
    } catch (error) {
      enterDemoMode();
    }
  }

  // Demo mode: this page is the project showcase, not someone's Budly instance.
  // Canned answers, clearly labelled, never implying access to a real Canvas.
  var DEMO_ANSWERS = [
    {
      re: /due this week|due/i,
      text:
        "**Demo answer**\n\nIn the real app, Budly answers from your synced Canvas data:\n" +
        '1. CSC 153 — Lab 5: Data Cleaning (due today, 11:59 pm)\n' +
        '2. COMP 214 — Group Project Milestone 2 (due tomorrow, 10:00 pm)\n' +
        '3. MATH 120 — Problem Set 6 (due Sunday)',
      sources: [
        { course: 'CSC 153', title: 'Lab 5: Data Cleaning', url: null },
        { course: 'COMP 214', title: 'Group Project Milestone 2', url: null },
      ],
    },
    {
      re: /new|changed|update/i,
      text:
        '**Demo answer**\n\nBudly detects changes between syncs and can tell you:\n' +
        '1. CSC 153 — new announcement: Module 6 released\n' +
        '2. COMP 214 — deadline moved: Milestone 2 (now next Monday)',
    },
    {
      re: /overdue/i,
      text: '**Demo answer**\n\n1. CSC 153 — Lab 4: SQL Basics (was due last week)',
    },
  ];

  function demoAnswer(text) {
    var lowered = (text || '').toLowerCase();
    for (var i = 0; i < DEMO_ANSWERS.length; i++) {
      if (DEMO_ANSWERS[i].re.test(lowered)) return DEMO_ANSWERS[i];
    }
    return {
      text:
        "This page is the project demo — it isn't connected to any Canvas account.\n\n" +
        'Download Budly, connect your own Canvas, and ask for real.',
    };
  }

  function enterDemoMode() {
    demoMode = true;
    var demo = document.createElement('span');
    demo.className = 'demobadge';
    demo.textContent = 'Demo';
    demo.title = 'Project showcase: not connected to any Canvas account';
    statusline.appendChild(demo);
  }

  function renderStatus(status) {
    var counts = status.counts || {};
    var dueToday = counts.due_today || 0;
    var overdue = counts.overdue || 0;
    var dueWeek = counts.due_week || 0;
    var line = document.createElement('span');
    line.textContent =
      dueWeek + ' due this week · ' + dueToday + ' today' + (overdue ? ' · ' + overdue + ' overdue' : '');
    statusline.innerHTML = '';
    statusline.appendChild(line);

    if (status.canvas && status.canvas.last_sync_error) {
      addMessage(
        'bot',
        "Budly lost access to Canvas.\n\nReconnect your Canvas account and I'll continue " +
          'syncing your courses.'
      );
    }

    // Mock mode is never silent: the badge travels with every status render.
    if (status.canvas && status.canvas.mock) {
      var demo = document.createElement('span');
      demo.className = 'demobadge';
      demo.textContent = 'Demo data';
      demo.title = 'CANVAS_MOCK_MODE is on — these are built-in fixtures, not your Canvas';
      statusline.appendChild(demo);
    }

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

  async function loadDigest() {
    try {
      var response = await fetch('/api/digests/latest');
      if (!response.ok) return;
      var digest = await response.json();
      var title = el('toast-title');
      var body = el('toast-body');
      var time = el('toast-time');
      if (!digest.fresh) {
        title.textContent = 'Digest sent!';
        time.textContent = formatTime(new Date(digest.sent_at));
      }
      var firstBullet = String(digest.body || '')
        .split('\n')
        .map(function (line) {
          return line
            .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // [label](url) -> label
            .replace(/\*\*/g, '')
            .trim();
        })
        .find(function (line) { return line.indexOf('- ') === 0; });
      body.textContent = firstBullet ? firstBullet.slice(2) : "You're clear for now.";
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
    if (demoMode) return;
    react('blink');
    try {
      var response = await fetch('/api/sync', { method: 'POST' });
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
      addMessage('bot', "Can't reach Budly right now.");
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

    if (demoMode) {
      thinking.remove();
      var demo = demoAnswer(text);
      addMessage('bot', renderMarkdown(demo.text), demo.sources);
      react('delighted', 900);
      setBusy(false);
      askinput.focus();
      return;
    }

    try {
      var response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text }),
      });
      thinking.remove();

      if (!response.ok) {
        var detail = '';
        try {
          detail = (await response.json()).detail || '';
        } catch (error) { /* non-JSON error body */ }
        addMessage('bot', detail || 'Something went wrong on the server. Try again in a moment.');
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
        "Can't reach Budly right now. Check your connection and try again."
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

  greet();
  init();
})();
